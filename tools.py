"""Fact-check Agent가 호출하는 도구 4종.

- symbol_lookup(name)   : 함수 존재 유무 (fact_check_tools 심볼 인덱스) — 결정론
- git_history_query(ref): 커밋 존재 유무 (git cat-file) — 결정론
- header_lookup(name)   : 헤더 파일 존재 유무 (파일명 대조) — 결정론
- function_call(call)   : 올바른 함수 사용법 — 보고서 원문 맥락으로 별도 LLM이 판단

symbol/git/header는 실제 코드베이스를 조회하고, function_call만 새 LLM 호출로 판단한다.
보고서 원문은 요청마다 set_report_context()로 컨텍스트에 저장해 function_call이 참조한다.
"""

import contextvars
import logging
import os
import subprocess
from pathlib import Path

from config import REPO_PATH, REPO_URL
from fact_check_tools import FactCheckTools
from function_call_checker import judge_function_calls

logger = logging.getLogger("fact_check_tools_service")

# 표준 C 라이브러리 헤더 (C89~C17). 대상 저장소에 없어도 "존재하는 표준 헤더"로 본다.
STANDARD_HEADERS = {
    "assert.h", "complex.h", "ctype.h", "errno.h", "fenv.h", "float.h",
    "inttypes.h", "iso646.h", "limits.h", "locale.h", "math.h", "setjmp.h",
    "signal.h", "stdalign.h", "stdarg.h", "stdatomic.h", "stdbit.h", "stdbool.h",
    "stdckdint.h", "stddef.h", "stdint.h", "stdio.h", "stdlib.h", "stdnoreturn.h",
    "string.h", "tgmath.h", "threads.h", "time.h", "uchar.h", "wchar.h", "wctype.h",
}

# POSIX / 시스템 헤더 (curl 리포트에 자주 등장). 대상 저장소에 없어도 "시스템 헤더"로 본다.
SYSTEM_HEADERS = {
    # POSIX 최상위
    "unistd.h", "fcntl.h", "poll.h", "pthread.h", "dlfcn.h", "dirent.h",
    "termios.h", "syslog.h", "sched.h", "semaphore.h", "pwd.h", "grp.h",
    "glob.h", "ifaddrs.h", "netdb.h", "strings.h", "libgen.h", "utime.h",
    "aio.h", "mqueue.h", "regex.h", "fnmatch.h", "sys/types.h",
    # sys/*
    "sys/socket.h", "sys/stat.h", "sys/time.h", "sys/select.h", "sys/wait.h",
    "sys/ioctl.h", "sys/mman.h", "sys/un.h", "sys/uio.h", "sys/resource.h",
    "sys/param.h", "sys/utsname.h", "sys/epoll.h", "sys/eventfd.h", "sys/file.h",
    # 네트워크
    "netinet/in.h", "netinet/tcp.h", "arpa/inet.h", "net/if.h", "netinet6/in6.h",
    # Windows (curl은 크로스플랫폼)
    "windows.h", "winsock2.h", "ws2tcpip.h", "wincrypt.h", "process.h", "io.h",
}
_SYSTEM_BASENAMES = {Path(h).name for h in SYSTEM_HEADERS}

# C++ 표준 라이브러리 헤더 (확장자 없음, C++11~C++23). 대상 저장소에 없어도 "존재하는 표준 헤더"로 본다.
# C 저장소(curl)에는 파일로 존재하지 않으므로, 이 목록으로 인식하지 않으면 not_found로 오탐된다.
CPP_STANDARD_HEADERS = {
    # 컨테이너
    "array", "deque", "forward_list", "list", "map", "queue", "set", "stack",
    "unordered_map", "unordered_set", "vector", "span", "flat_map", "flat_set", "mdspan",
    # 문자열
    "string", "string_view", "charconv", "format",
    # 입출력
    "iostream", "istream", "ostream", "fstream", "sstream", "iomanip", "ios", "iosfwd",
    "streambuf", "syncstream", "spanstream", "print",
    # 유틸리티/일반
    "utility", "tuple", "optional", "variant", "any", "bitset", "functional", "memory",
    "memory_resource", "scoped_allocator", "type_traits", "typeindex", "typeinfo",
    "initializer_list", "compare", "coroutine", "source_location", "expected", "generator",
    # 수치
    "numeric", "complex", "valarray", "random", "ratio", "bit", "numbers",
    # 알고리즘/반복자/범위
    "algorithm", "execution", "ranges", "iterator", "concepts",
    # 스레드/동시성
    "thread", "mutex", "shared_mutex", "condition_variable", "future", "atomic",
    "stop_token", "barrier", "latch", "semaphore",
    # 시간/로케일
    "chrono", "locale", "codecvt",
    # 진단/예외
    "exception", "stdexcept", "system_error",
    # 정규식/파일시스템
    "regex", "filesystem",
    # C 호환 헤더 (C++판)
    "cassert", "cctype", "cerrno", "cfenv", "cfloat", "cinttypes", "climits", "clocale",
    "cmath", "csetjmp", "csignal", "cstdarg", "cstddef", "cstdint", "cstdio", "cstdlib",
    "cstring", "ctime", "cuchar", "cwchar", "cwctype",
}

_engine = None

# 요청별 격리를 위해 ContextVar 사용
_report_txt = contextvars.ContextVar("fact_check_report_txt", default="")
# 함수 호출 배열 전체를 1회 LLM 호출로 사전 판단한 결과 (call -> 판단 dict)
_fc_results = contextvars.ContextVar("fact_check_fc_results", default=None)


def set_report_context(raw_report_txt: str) -> None:
    _report_txt.set(raw_report_txt or "")


def get_function_call_results() -> list:
    """prime_function_calls가 사전 판단한 함수 호출 결과 목록을 반환한다.

    시스템이 function_call_check 슬롯을 결정론적으로 채우는 데 쓴다(LLM 전사 비의존).
    사전 판단이 없거나 함수 호출이 없었으면 []를 반환한다.
    """
    cache = _fc_results.get()
    if not cache:
        return []
    return list(cache.values())


def prime_function_calls(raw_report_txt: str, calls, signatures=None, user_defined_functions=None) -> list:
    """함수 호출 배열 '전체'를 한 번의 LLM 호출로 사전 판단해 컨텍스트에 저장한다.

    signatures: 코드베이스에서 추출한 라이브러리 함수 실제 시그니처 맵({함수명: "시그니처"|"NOT_FOUND"}).
    user_defined_functions: 리포터가 직접 정의한 함수명 목록(정의는 raw_report_txt 안).
    이후 function_call 도구는 이 결과를 조회만 하므로, /invoke 1건당 함수 사용법 LLM 호출은 1회다.
    반환: [{call, valid, reason, confidence}, ...]  (실패 시 빈 목록으로 저장)
    """
    set_report_context(raw_report_txt)
    try:
        results = judge_function_calls(  # ← 유일한 LLM 호출
            raw_report_txt, calls,
            signatures=signatures, user_defined_functions=user_defined_functions,
        )
    except Exception as exc:  # noqa: BLE001 - 사전 판단 실패가 전체를 막지 않도록
        logger.warning("function_call 사전 판단 실패: %s", exc)
        results = []
    _fc_results.set({r.get("call"): r for r in results if isinstance(r, dict) and r.get("call")})
    return results


def ensure_repo() -> None:
    """대상 저장소를 준비한다.

    커밋 존재 조회(git_history_query)가 동작하려면 '전체 히스토리'가 필요하다. 따라서
    - repo가 없으면 full clone(--depth 없음, 모든 브랜치)한다.
    - 기존 repo가 shallow면 unshallow하여 전체 히스토리를 확보한다.
      (shallow 클론이면 과거 커밋이 없어 실제 커밋도 '없음'으로 오탐되기 때문)
    """
    git_dir = os.path.join(REPO_PATH, ".git")
    if os.path.isdir(git_dir):
        try:
            shallow = subprocess.run(
                ["git", "-C", REPO_PATH, "rev-parse", "--is-shallow-repository"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            if shallow == "true":
                logger.info("기존 저장소가 shallow → unshallow(전체 히스토리) 진행: %s", REPO_PATH)
                subprocess.run(
                    ["git", "-C", REPO_PATH, "fetch", "--unshallow", "--tags"],
                    check=False, timeout=600,
                )
            else:
                # stale clone이면 리포트가 인용한 '최신' 커밋을 놓쳐 실재 커밋을 not-found로
                # 오탐할 수 있다. best-effort로 최신 이력을 받아둔다(오프라인이면 무시).
                logger.info("저장소 최신화(git fetch --tags): %s", REPO_PATH)
                subprocess.run(
                    ["git", "-C", REPO_PATH, "fetch", "--tags", "--quiet"],
                    check=False, timeout=300,
                )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("저장소 최신화 실패(무시): %s", exc)
        return

    if os.path.isdir(REPO_PATH) and os.listdir(REPO_PATH):
        return  # git이 아닌 내용이 이미 있음(볼륨 마운트 등)

    if REPO_URL:
        logger.info("REPO_PATH가 비어 있어 full clone: %s → %s", REPO_URL, REPO_PATH)
        os.makedirs(REPO_PATH, exist_ok=True)
        # --depth를 주지 않아 전체 히스토리 + 모든 브랜치를 받는다(커밋 조회에 필수).
        subprocess.run(["git", "clone", REPO_URL, REPO_PATH], check=True)


def get_engine() -> FactCheckTools:
    """FactCheckTools를 지연 초기화한다(대상 저장소 인덱스 1회 빌드)."""
    global _engine
    if _engine is None:
        ensure_repo()
        logger.info("저장소 인덱싱 시작: %s", REPO_PATH)
        _engine = FactCheckTools(REPO_PATH)
        logger.info("인덱싱 완료.")
    return _engine


# --------------------------------------------------------------------------
# 도구 (LLM이 function calling으로 호출)
# --------------------------------------------------------------------------

def symbol_lookup(name: str) -> dict:
    """함수/심볼이 코드베이스에 실제로 존재하는지 조회한다. 존재하면 위치(file:line)를 반환한다."""
    return get_engine().symbol_lookup(name)


def signature_lookup(name: str):
    """코드베이스의 함수 정의에서 시그니처 문자열을 추출한다(function_call 판단용). 없으면 None.

    예: "curl_url_cleanup" → "void curl_url_cleanup(CURLU *handle)"
    """
    res = get_engine().symbol_lookup(name)
    loc = res.get("location")
    if not res.get("exists") or not loc or ":" not in loc:
        return None
    file_part, _, line_part = loc.rpartition(":")
    if not line_part.isdigit():
        return None
    path = Path(REPO_PATH) / file_part
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None
    start = int(line_part) - 1
    if start < 0 or start >= len(lines):
        return None
    chunk = "\n".join(lines[start : start + 15])  # 시그니처가 여러 줄에 걸칠 수 있음
    idx = chunk.find(name)
    if idx == -1:
        return None
    paren = chunk.find("(", idx)
    if paren == -1:
        return None
    depth = 0
    end = -1
    for i in range(paren, len(chunk)):
        c = chunk[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    return " ".join(chunk[: end + 1].split())  # 공백/개행 정리한 시그니처


def git_history_query(ref: str) -> dict:
    """커밋 해시 또는 ref가 저장소 git 이력에 실제로 존재하는지 조회한다."""
    return get_engine().git_history_query(ref)


def header_lookup(name: str) -> dict:
    """헤더 파일이 존재하는지 조회한다.

    - 표준 C 라이브러리 헤더(stdio.h 등)는 존재로 본다(kind="standard_library").
    - C++ 표준 라이브러리 헤더(iostream, thread 등 확장자 없는 것)도 존재로 본다(kind="cpp_standard_library").
    - POSIX/시스템 헤더(unistd.h, sys/socket.h 등)도 존재로 본다(kind="system").
    - 그 외에는 대상 저장소(REPO_PATH)에서 파일명으로 찾는다(kind="project" / "not_found").
    - 경로/괄호/따옴표가 붙어 와도 정규화한다: "<stdio.h>", "curl/curl.h", '"curl_printf.h"' 등.
    """
    raw = (name or "").strip()
    cleaned = raw.strip("<>\"' ")          # "<sys/socket.h>" → "sys/socket.h"
    base = Path(cleaned).name              # "curl/curl.h" → "curl.h"

    if base in STANDARD_HEADERS:
        return {"name": raw, "exists": True, "kind": "standard_library", "location": None}

    if cleaned in CPP_STANDARD_HEADERS:
        return {"name": raw, "exists": True, "kind": "cpp_standard_library", "location": None}

    if cleaned in SYSTEM_HEADERS or base in _SYSTEM_BASENAMES:
        return {"name": raw, "exists": True, "kind": "system", "location": None}

    result = get_engine().existence_checker.check_by_basename(base)
    result["name"] = raw
    result["kind"] = "project" if result.get("exists") else "not_found"
    return result


def function_call(call: str) -> dict:
    """함수 호출이 올바르게 사용되었는지에 대한 판단을 반환한다. valid/reason/confidence.

    함수 호출 배열은 prime_function_calls()에서 이미 1회 LLM 호출로 판단해 두었으므로,
    이 도구는 그 결과를 조회만 한다(추가 LLM 호출 없음).
    """
    call = (call or "").strip()
    cache = _fc_results.get()

    # 판단 불가는 valid=true로 처리한다(무죄 추정).
    if cache is not None:
        if call in cache:
            return cache[call]
        return {"call": call, "valid": True, "reason": "사전 판단 목록에 없어 판단 불가 → valid 처리", "confidence": 0.3}

    # prime이 호출되지 않은 경우(예: 도구 직접 사용)의 방어 — 단건 판단
    try:
        results = judge_function_calls(_report_txt.get(), [call])
    except Exception as exc:  # noqa: BLE001
        logger.warning("function_call LLM 판단 실패: %s", exc)
        return {"call": call, "valid": True, "reason": f"판단 불가 → valid 처리: {exc}", "confidence": 0.3}
    if results:
        r = dict(results[0])
        r.setdefault("call", call)
        return r
    return {"call": call, "valid": True, "reason": "LLM 결과 없음 → valid 처리", "confidence": 0.3}
