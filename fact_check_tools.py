"""
fact_check_tools.py

버그바운티 리포트 검증 파이프라인 - "사실 판단 Agent" 담당 모듈.
Parser Agent가 추출한 cited_functions / cited_commits를 실제 코드베이스와
git 이력에 대조하여 존재 여부를 결정론적으로 확인한다.

설계 원칙 (기획서 "Deterministic-first", "Evidence-first" 반영)
------------------------------------------------------------
1. LLM 추론/기억에 의존하지 않는다. 반드시 ctags/grep/git 조회 결과만 근거로 삼는다.
2. 리포트 본문에서 추출된 문자열(함수명, 커밋 해시)은 "신뢰할 수 없는 입력"으로 간주하고,
   실행 전 화이트리스트 정규식으로 검증한다 (프롬프트 인젝션이 아닌 "커맨드 인젝션" 방어).
3. subprocess는 shell=False + 리스트 인자로만 호출한다. 문자열 조합으로 셸 명령을 만들지 않는다.
4. 모든 외부 프로세스 호출에는 timeout을 건다 (PoC/리포트가 무한 루프를 유발할 수 있음을 가정).
5. Judge Agent / 찬반 Agent가 기대하는 출력 스키마와 1:1로 맞춘다.
   {"name":..., "exists":..., "location":...}
   {"ref":...,  "exists":..., "reason":...}
"""

from __future__ import annotations

import logging
import re
import shutil
import difflib
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger("fact_check_agent")

_GITHUB_SLUG_RE = re.compile(r"github\.com[:/]+([^/]+)/([^/.\s]+)")


@lru_cache(maxsize=8)
def _remote_github_slug(repo_path: str):
    """저장소 origin 원격이 GitHub이면 (owner, repo)를 반환한다(아니면 None)."""
    proc = _run(["git", "remote", "get-url", "origin"], cwd=Path(repo_path))
    if proc.returncode != 0:
        return None
    m = _GITHUB_SLUG_RE.search(proc.stdout.strip())
    return (m.group(1), m.group(2)) if m else None


@lru_cache(maxsize=2048)
def _github_commit_exists(repo_path: str, ref: str):
    """원격 GitHub에 해당 commit이 존재하는지 확인한다.

    로컬 메인라인 clone에는 없지만(도달 불가) 원격엔 실재하는 커밋(pre-merge/PR/리베이스 전 SHA)을
    가려내기 위한 best-effort 폴백. 반환: True(존재)/False(없음)/None(확인 불가: 네트워크/rate limit).
    축약 SHA도 GitHub API가 해석하므로 함께 처리된다.
    """
    slug = _remote_github_slug(repo_path)
    if not slug:
        return None
    owner, repo = slug
    api = f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}"
    req = urllib.request.Request(
        api, headers={"Accept": "application/vnd.github+json", "User-Agent": "fact-check-agent"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return False  # 존재하지 않는/유효하지 않은 ref
        return None  # 403(rate limit) 등은 '확인 불가'
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        logger.warning("GitHub commit 조회 실패(무시): %s", e)
        return None


# --------------------------------------------------------------------------
# 예외
# --------------------------------------------------------------------------

class RepoError(Exception):
    """저장소 접근 또는 도구 실행 실패."""


class UnsafeInputError(Exception):
    """검증 대상 문자열이 허용 패턴을 벗어남 (인젝션 의심)."""


# --------------------------------------------------------------------------
# 입력 검증 (커맨드 인젝션 방어)
# --------------------------------------------------------------------------

# 함수/심볼명: 대부분 언어의 식별자 + C++ 네임스페이스(::), 템플릿 일부 허용
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_:.$~]{0,127}$")

# 커밋 ref: 4~40자리 hex SHA, 또는 안전한 refname 문자만 허용
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{4,40}$")
_REFNAME_PATTERN = re.compile(r"^[A-Za-z0-9_./\-]{1,255}$")


def validate_symbol_name(name: str) -> str:
    name = (name or "").strip()
    if not name or not _SYMBOL_PATTERN.match(name):
        raise UnsafeInputError(f"허용되지 않는 심볼명 형식: {name!r}")
    return name


def validate_commit_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if ref.startswith("-"):
        # "-- upload-pack=..." 류의 git 옵션 인젝션 시도 차단
        raise UnsafeInputError(f"옵션 인젝션 의심 ref: {ref!r}")
    if _COMMIT_SHA_PATTERN.match(ref) or _REFNAME_PATTERN.match(ref):
        return ref
    raise UnsafeInputError(f"허용되지 않는 commit ref 형식: {ref!r}")


def _run(cmd: List[str], cwd: Path, timeout: int = 10) -> subprocess.CompletedProcess:
    """모든 git/ctags 호출의 단일 진입점. shell=False 고정."""
    logger.debug("exec: %s (cwd=%s)", cmd, cwd)
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RepoError(f"명령 시간 초과: {' '.join(cmd)}") from e
    except FileNotFoundError as e:
        raise RepoError(f"실행 파일을 찾을 수 없음: {cmd[0]}") from e


# --------------------------------------------------------------------------
# 출력 스키마 (기획서 fact_check_result 항목과 매핑)
# --------------------------------------------------------------------------

@dataclass
class FunctionCheckResult:
    name: str
    exists: bool
    location: Optional[str] = None  # 예: "lib/url.c:123"

    def to_dict(self) -> dict:
        return {"name": self.name, "exists": self.exists, "location": self.location}


@dataclass
class CommitCheckResult:
    ref: str
    exists: bool
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        d: dict = {"ref": self.ref, "exists": self.exists}
        d["reason"] = self.reason
        return d


# --------------------------------------------------------------------------
# 함수 심볼 인덱서 (symbol_lookup 백엔드)
# --------------------------------------------------------------------------

class SymbolIndexer:
    """
    저장소 내 함수/심볼 정의 위치 인덱스.

    1순위: universal-ctags (-R --fields=+n, 언어 다수 지원, 정확도 높음)
    2순위: ctags 미설치 시 언어별 정규식 grep fallback (정확도는 낮지만 서비스 가용성 보장)

    인덱스는 프로세스 내 메모리에 캐싱한다. 대규모 저장소에서는 리포트 1건마다
    재빌드하지 말고, 서비스 기동 시 1회 빌드 + git push webhook 시점에 refresh(force=True)
    하는 방식을 권장한다 (아래 __main__ 예시 참고).
    """

    _DEF_PATTERNS = [
        # Python / JS / Go처럼 시그니처가 항상 한 줄에 끝나는 언어
        re.compile(r"^\s*def\s+(\w+)\s*\("),                    # Python
        re.compile(r"^\s*(?:export\s+)?function\s+(\w+)\s*\("),  # JS/TS
        re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("),   # Go
    ]
    # C/C++/Java류 함수 정의: 시그니처와 여는 중괄호가 여러 줄에 걸쳐 있는 경우까지 포함해서
    # 탐지한다 (curl처럼 K&R 스타일로 '{'가 다음 줄에 오거나, 매개변수 목록 자체가
    # 다음 줄로 넘어가는 경우 둘 다 대응). DOTALL로 여러 줄을 하나의 문자열처럼 취급한다.
    #   예1) static CURLUcode seturl(const char *url, CURLU *u, unsigned int flags)
    #        {
    #   예2) CURLUcode curl_url_set(CURLU *u, CURLUPart what,
    #                              const char *part, unsigned int flags)
    #        {
    _MULTILINE_DEF_PATTERN = re.compile(
        r"^[\w][\w\s\*&:<>]*?\b(\w+)\s*\(([^;{}]*)\)\s*\{", re.DOTALL,
    )
    _FUNC_DEF_WINDOW = 8  # 시그니처가 이 줄 수 안에서 끝난다고 가정 (그 이상은 매크로/이상 케이스로 보고 포기)
    _FALLBACK_EXTS = {".c", ".h", ".cc", ".cpp", ".hpp", ".py", ".js", ".ts", ".go", ".java"}

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        if not self.repo_path.exists():
            raise RepoError(f"저장소 경로가 존재하지 않음: {repo_path}")
        self._index: Dict[str, List[dict]] = {}
        self._built = False
        self._has_ctags = shutil.which("ctags") is not None

    def build(self, force: bool = False) -> None:
        if self._built and not force:
            return
        self._index.clear()
        if self._has_ctags:
            self._build_with_ctags()
        else:
            logger.warning("ctags 미설치 - 정규식 fallback 인덱서 사용 (정확도 낮음)")
            self._build_with_grep_fallback()
        self._built = True
        logger.info("심볼 인덱스 빌드 완료: %d개 고유 심볼", len(self._index))

    def _build_with_ctags(self) -> None:
        cmd = [
            "ctags", "-R",
            "--fields=+n",
            "--languages=all",
            "-f", "-",  # stdout
            str(self.repo_path),
        ]
        proc = _run(cmd, cwd=self.repo_path, timeout=180)
        if proc.returncode != 0:
            raise RepoError(f"ctags 실행 실패: {proc.stderr.strip()}")

        for line in proc.stdout.splitlines():
            if not line or line.startswith("!_TAG_"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name, file_path = parts[0], parts[1]
            line_no = None
            for extra in parts[3:]:
                if extra.startswith("line:"):
                    line_no = extra.split(":", 1)[1]
                    break
            self._index.setdefault(name, []).append(
                {"file": self._relativize(file_path), "line": line_no}
            )

    def _build_with_grep_fallback(self) -> None:
        for path in self.repo_path.rglob("*"):
            if not path.is_file() or path.suffix not in self._FALLBACK_EXTS:
                continue
            try:
                lines = path.read_text(errors="ignore").splitlines()
            except OSError:
                continue

            for i, line in enumerate(lines):
                matched = False
                for pat in self._DEF_PATTERNS:
                    m = pat.search(line)
                    if m:
                        self._index.setdefault(m.group(1), []).append(
                            {"file": self._relativize(str(path)), "line": str(i + 1)}
                        )
                        matched = True
                        break
                if matched:
                    continue

                # 시그니처 "시작 후보"만 최소 비용으로 필터링: 여는 괄호가 있어야 하고,
                # "세미콜론은 있는데 중괄호가 전혀 없는" 순수 프로토타입 선언만 제외한다.
                # (한 줄짜리 함수는 본문에도 ';'가 있으므로 단순히 ';' 존재만으로 걸러내면 안 된다)
                if "(" not in line:
                    continue
                if ";" in line and "{" not in line:
                    continue
                window = "\n".join(lines[i: i + self._FUNC_DEF_WINDOW])
                m = self._MULTILINE_DEF_PATTERN.match(window)
                if m:
                    self._index.setdefault(m.group(1), []).append(
                        {"file": self._relativize(str(path)), "line": str(i + 1)}
                    )


    def _relativize(self, path: str) -> str:
        try:
            return str(Path(path).resolve().relative_to(self.repo_path))
        except ValueError:
            return path

    def _find_in_history(self, name: str) -> bool:
        """과거 모든 커밋의 소스코드 이력에서 함수명이 등장하는지 확인"""
        if not (self.repo_path / ".git").exists():
            return False
        cmd = ["git", "log", "-S", name, "--oneline", "-n", "1"]
        try:
            proc = _run(cmd, cwd=self.repo_path, timeout=10)
            return proc.returncode == 0 and bool(proc.stdout.strip())
        except Exception:
            return False

    def lookup(self, name: str) -> FunctionCheckResult:
        self.build()  # 이미 빌드됐으면 no-op
        entries = self._index.get(name)
        if entries:
            first = entries[0]
            loc = f"{first['file']}:{first['line']}" if first.get("line") else first["file"]
            return FunctionCheckResult(name=name, exists=True, location=loc)

        # 1. Git 이력 조회 (과거 버전에 존재했는지 검증)
        if self._find_in_history(name):
            logger.info("Found '%s' in git history (past version)", name)
            return FunctionCheckResult(name=name, exists=True, location="git history (deleted/renamed)")

        # 2. 오타 교정 (유사한 함수 매칭)
        matches = difflib.get_close_matches(name, self._index.keys(), n=1, cutoff=0.5)
        if matches:
            matched_name = matches[0]
            print(f"❌ {name}은(는) 존재하지 않습니다. 혹시 '{matched_name}'을(를) 의미하신 건가요?")
            # 내부적으로 matched_name으로 치환해서 검증을 계속 진행
            corrected_result = self.lookup(matched_name)
            return FunctionCheckResult(
                name=f"{name} (typo corrected to {matched_name})",
                exists=corrected_result.exists,
                location=corrected_result.location
            )

        return FunctionCheckResult(name=name, exists=False, location=None)


# --------------------------------------------------------------------------
# 커밋 검증 (git_history_query 백엔드)
# --------------------------------------------------------------------------

class GitCommitChecker:
    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path).resolve()
        if not (self.repo_path / ".git").exists():
            raise RepoError(f".git 디렉터리를 찾을 수 없음: {self.repo_path}")

    def refresh(self, timeout: int = 60) -> None:
        """최근 커밋을 리포트가 인용했는데 로컬 클론이 오래돼 놓치는 일을 막기 위한 fetch.
        운영에서는 매 리포트마다 fetch하지 말고 주기적 cron/webhook으로 갱신 권장."""
        proc = _run(["git", "fetch", "--all", "--tags"], cwd=self.repo_path, timeout=timeout)
        if proc.returncode != 0:
            logger.warning("git fetch 실패 (오프라인/미러 환경일 수 있음): %s", proc.stderr.strip())

    @lru_cache(maxsize=1024)
    def _exists_in_history(self, ref: str) -> bool:
        # ^{commit} : 태그가 커밋을 가리키는 경우까지 포함해 "커밋으로서" 존재하는지 확인
        proc = _run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], cwd=self.repo_path)
        return proc.returncode == 0

    def check(self, ref: str) -> CommitCheckResult:
        try:
            safe_ref = validate_commit_ref(ref)
        except UnsafeInputError as e:
            return CommitCheckResult(ref=ref, exists=False, reason=str(e))

        if self._exists_in_history(safe_ref):
            return CommitCheckResult(ref=ref, exists=True, reason=None)

        # 폴백: 로컬 메인라인 clone엔 도달 불가능하지만 원격(GitHub)엔 실재하는 커밋
        # (병합 전/PR/리베이스 전 SHA)을 가려낸다. 확인 불가(네트워크/rate limit)면 not-found 유지.
        # 단, 충분히 긴 hex SHA(>=12)만 조회한다. 짧은 접두사(예: 'abc123')는 우연히 임의 커밋과
        # 매칭돼 오탐이 나므로 제외한다(플레이스홀더/오추출 방어).
        remote = None
        if re.fullmatch(r"[0-9a-fA-F]{12,40}", safe_ref):
            remote = _github_commit_exists(str(self.repo_path), safe_ref)
        if remote is True:
            return CommitCheckResult(
                ref=ref, exists=True,
                reason="로컬 메인라인 이력엔 없으나 원격 저장소에 실재하는 commit(병합 전/PR/리베이스 전 SHA)",
            )
        return CommitCheckResult(
            ref=ref, exists=False,
            reason="저장소 git 이력에서 해당 commit을 찾을 수 없음",
        )


# --------------------------------------------------------------------------
# 파일/헤더류 존재 확인 (file_check, header_check 백엔드)
# --------------------------------------------------------------------------

class ExistenceChecker:
    """
    파일 경로/헤더처럼 "저장소 안에 이 경로(또는 이름)가 있는가"만 확인하면 되는
    체크 항목들의 공용 백엔드. rglob 결과를 1회 캐싱해두고 재사용한다.
    """

    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self._file_cache: Optional[set] = None

    def _all_files(self) -> set:
        if self._file_cache is None:
            self._file_cache = {
                str(p.relative_to(self.repo_path))
                for p in self.repo_path.rglob("*")
                if p.is_file()
            }
        return self._file_cache

    def refresh(self) -> None:
        self._file_cache = None

    def check_exact_path(self, rel_path: str) -> dict:
        """리포트에 정확한 상대경로가 적힌 경우 (예: lib/url.c)."""
        rel_path = (rel_path or "").strip().lstrip("/")
        exists = rel_path in self._all_files()
        return {"path": rel_path, "exists": exists}

    def check_by_basename(self, name: str) -> dict:
        """헤더처럼 정확한 경로 없이 파일명만 언급되는 경우 (예: url.h)."""
        name = (name or "").strip()
        matches = [f for f in self._all_files() if Path(f).name == name]
        return {
            "name": name,
            "exists": bool(matches),
            "location": matches[0] if matches else None,
        }


# --------------------------------------------------------------------------
# 체크 항목 팩토리 - "요소별 체크 함수를 빠르게 늘리는" 핵심
# --------------------------------------------------------------------------

class CheckRegistry:
    """
    체크 항목(function/commit/file/header/... )을 이름으로 등록해두고
    공통 로직(예외 처리, 로깅, dict 변환)을 한 곳에서 감싸는 팩토리.

    새 체크 항목 추가 = 함수 하나 작성 + register() 한 줄.
    예)
        @registry.register("license_header")
        def _check_license(text: str) -> dict:
            return {"exists": "SPDX-License-Identifier" in text}
    """

    def __init__(self):
        self._registry: Dict[str, Callable[[str], dict]] = {}

    def register(self, key: str) -> Callable:
        def decorator(fn: Callable[[str], dict]) -> Callable[[str], dict]:
            self._registry[key] = fn
            return fn
        return decorator

    def run(self, key: str, value: str) -> dict:
        fn = self._registry.get(key)
        if fn is None:
            raise KeyError(f"등록되지 않은 체크 종류: '{key}' (사용 가능: {list(self._registry)})")
        try:
            return fn(value)
        except UnsafeInputError as e:
            return {"input": value, "exists": False, "error": str(e)}
        except RepoError as e:
            logger.error("체크 실행 실패 [%s=%s]: %s", key, value, e)
            return {"input": value, "exists": False, "error": f"내부 오류: {e}"}

    def run_batch(self, key: str, values: Iterable[str]) -> List[dict]:
        seen = dict.fromkeys(values)  # 순서 보존 + 중복 제거
        return [self.run(key, v) for v in seen]

    @property
    def available_checks(self) -> List[str]:
        return list(self._registry.keys())


# --------------------------------------------------------------------------
# 호출 체인 도달 가능성 검증 (reachability 백엔드)
# --------------------------------------------------------------------------

class ReachabilityChecker:
    """
    "함수 A의 본문 안에 함수 B에 대한 호출이 실제로 등장하는가"를
    LLM 없이 정적으로(텍스트 레벨에서) 검증한다.

    SymbolIndexer가 이미 확보한 함수 위치(file:line)를 재사용해서 함수 본문을
    중괄호 깊이 카운팅으로 추출하고, 그 안에서 callee(...) 호출 패턴을 찾는다.
    호출 체인 [A, B, C]가 주어지면 A->B, B->C 두 구간을 모두 검사해서
    REACHABLE / UNREACHABLE / UNKNOWN을 판단한다.

    한계 (정직하게 밝혀둠):
    - 함수 포인터를 통한 간접 호출, 매크로로 감춰진 호출은 잡지 못한다.
    - 조건문 안쪽 호출도 "등장은 한다"로 잡히므로, 실제 조건이 항상 참인지는 보장하지 않는다.
      즉 이 체커가 내는 REACHABLE은 "정적으로 그런 호출이 코드에 존재한다"는 의미이지,
      "모든 입력에서 반드시 실행된다"는 의미는 아니다. Judge Agent 프롬프트에도 이 뉘앙스를
      reason에 반영해야 한다.
    """

    _MAX_BODY_LINES = 2000  # 비정상적으로 긴 함수(매크로 붙은 파일 등)로부터의 방어

    def __init__(self, repo_path: Path, symbol_indexer: "SymbolIndexer"):
        self.repo_path = repo_path
        self.symbol_indexer = symbol_indexer

    def _extract_function_body(self, name: str) -> Optional[str]:
        result = self.symbol_indexer.lookup(name)
        if not result.exists or not result.location:
            return None

        file_part, _, line_part = result.location.rpartition(":")
        if not file_part or not line_part.isdigit():
            return None

        file_path = self.repo_path / file_part
        try:
            lines = file_path.read_text(errors="ignore").splitlines()
        except OSError:
            return None

        start_idx = int(line_part) - 1  # location의 line은 1-based
        if start_idx < 0 or start_idx >= len(lines):
            return None

        depth = 0
        started = False
        body_lines: List[str] = []
        for line in lines[start_idx: start_idx + self._MAX_BODY_LINES]:
            body_lines.append(line)
            depth += line.count("{") - line.count("}")
            if "{" in line:
                started = True
            if started and depth <= 0:
                break
        if not started:
            return None
        return "\n".join(body_lines)

    def is_call_reachable(self, caller: str, callee: str) -> dict:
        body = self._extract_function_body(caller)
        if body is None:
            return {
                "caller": caller,
                "callee": callee,
                "reachable": None,
                "reason": f"'{caller}' 함수의 본문을 코드베이스에서 찾지 못해 확인 불가",
            }
        call_pattern = re.compile(r"\b" + re.escape(callee) + r"\s*\(")
        found = bool(call_pattern.search(body))
        return {
            "caller": caller,
            "callee": callee,
            "reachable": found,
            "reason": (
                f"'{caller}' 함수 본문 안에서 '{callee}(' 호출을 확인함"
                if found
                else f"'{caller}' 함수 본문에서 '{callee}(' 호출을 찾지 못함"
            ),
        }

    def verify_call_chain(self, chain: List[str]) -> dict:
        """
        chain 예: ["curl_url_set", "parseurl", "seturl"]
        (공격자 입력에 가까운 함수부터 최종 sink 함수 순서로 나열한다고 가정)
        """
        if len(chain) < 2:
            return {"verdict": "UNKNOWN", "reason": "체인 길이가 2 미만이라 검증할 구간이 없음", "steps": []}

        steps = [self.is_call_reachable(caller, callee) for caller, callee in zip(chain, chain[1:])]

        if any(s["reachable"] is None for s in steps):
            verdict = "UNKNOWN"
            missing = [s["caller"] for s in steps if s["reachable"] is None]
            reason = f"다음 함수의 본문을 찾지 못해 전체 경로를 확정할 수 없음: {', '.join(missing)}"
        elif all(s["reachable"] for s in steps):
            verdict = "REACHABLE"
            reason = " -> ".join(chain) + " 순서의 호출이 소스코드 상에서 정적으로 확인됨 (조건부 실행 여부는 별도 확인 필요)"
        else:
            broken = next(s for s in steps if s["reachable"] is False)
            verdict = "UNREACHABLE"
            reason = f"'{broken['caller']}' -> '{broken['callee']}' 구간에서 호출이 확인되지 않아 체인이 끊어짐"

        return {"verdict": verdict, "reason": reason, "steps": steps}


# --------------------------------------------------------------------------
# Agent 진입점 - 기획서의 symbol_lookup / git_history_query 도구 시그니처
# --------------------------------------------------------------------------

class FactCheckTools:
    """
    사실 판단 Agent가 tool-use 단계에서 호출하는 진입점.
    LangGraph / Anthropic tool_use 스펙의 함수 시그니처와 그대로 맞춘다.
    """

    def __init__(self, repo_path: str, auto_refresh: bool = False):
        repo = Path(repo_path).resolve()
        self.symbol_indexer = SymbolIndexer(repo_path)
        self.commit_checker = GitCommitChecker(repo_path)
        self.existence_checker = ExistenceChecker(repo)
        self.reachability_checker = ReachabilityChecker(repo, self.symbol_indexer)
        if auto_refresh:
            self.commit_checker.refresh()
        # 인덱스는 서비스 기동 시 1회 빌드 (요청마다 재빌드하지 않음)
        self.symbol_indexer.build()

        # ---- 체크 항목 등록: 새 항목은 여기 3줄만 추가하면 됨 ----
        self.registry = CheckRegistry()
        self.registry.register("function")(self.symbol_lookup)
        self.registry.register("commit")(self.git_history_query)
        self.registry.register("file")(self.existence_checker.check_exact_path)
        self.registry.register("header")(self.existence_checker.check_by_basename)

    # ---- 단건 조회 (Agent 도구 스펙과 1:1 매칭) ----

    def verify_reachability(self, chain: List[str]) -> dict:
        """
        function_calls 순서 등에서 추출한 (추정) 호출 체인을 검증한다.
        fact_check_result.reachability 필드에 그대로 대입 가능한 형태를 반환한다.
        """
        validated_chain = []
        for name in chain:
            try:
                validated_chain.append(validate_symbol_name(name))
            except UnsafeInputError as e:
                return {"verdict": "UNKNOWN", "reason": str(e), "steps": []}
        return self.reachability_checker.verify_call_chain(validated_chain)


    def symbol_lookup(self, name: str) -> dict:
        try:
            name = validate_symbol_name(name)
        except UnsafeInputError as e:
            logger.warning("symbol_lookup 거부: %s", e)
            return {"name": name, "exists": False, "location": None, "reason": str(e)}
        return self.symbol_indexer.lookup(name).to_dict()

    def git_history_query(self, ref: str) -> dict:
        return self.commit_checker.check(ref).to_dict()

    def refresh_all(self) -> None:
        """git push webhook 등에서 호출 - 인덱스/파일 캐시 전체 재빌드."""
        self.commit_checker.refresh()
        self.symbol_indexer.build(force=True)
        self.existence_checker.refresh()

    # ---- Parser Agent 결과 배치 검증 (function_check / commit_check 배열 생성) ----

    def check_functions(self, names: Iterable[str]) -> List[dict]:
        seen = dict.fromkeys(names)  # 순서 보존 + 중복 제거
        return [self.symbol_lookup(n) for n in seen]

    def check_commits(self, refs: Iterable[str]) -> List[dict]:
        seen = dict.fromkeys(refs)
        return [self.git_history_query(r) for r in seen]


# --------------------------------------------------------------------------
# Claude / LangGraph tool 스펙 (참고용) - 실제 tools 파라미터에 그대로 사용 가능
# --------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "name": "symbol_lookup",
        "description": "함수 또는 심볼이 코드베이스에 실제로 존재하는지, 존재한다면 위치(file:line)를 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "조회할 함수/심볼 이름"}},
            "required": ["name"],
        },
    },
    {
        "name": "git_history_query",
        "description": "커밋 해시 또는 ref가 저장소 git 이력에 실제로 존재하는지 조회한다.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "조회할 커밋 SHA 또는 ref"}},
            "required": ["ref"],
        },
    },
]


# --------------------------------------------------------------------------
# 데모 실행
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) < 2:
        print("사용법: python fact_check_tools.py <repo_path> [function_name] [commit_ref]")
        sys.exit(1)

    repo = sys.argv[1]
    fn = sys.argv[2] if len(sys.argv) > 2 else "main"
    commit = sys.argv[3] if len(sys.argv) > 3 else "HEAD"

    tools = FactCheckTools(repo)

    result = {
        "function_check": tools.check_functions([fn, "definitely_not_a_real_function_xyz"]),
        "commit_check": tools.check_commits([commit, "deadbeef00"]),
        "header_check": tools.registry.run_batch("header", ["url.h", "nope_no_such_header.h"]),
        "available_checks": tools.registry.available_checks,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
