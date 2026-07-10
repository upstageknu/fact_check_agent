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

_engine = None

# 요청별 격리를 위해 ContextVar 사용
_report_txt = contextvars.ContextVar("fact_check_report_txt", default="")
# 함수 호출 배열 전체를 1회 LLM 호출로 사전 판단한 결과 (call -> 판단 dict)
_fc_results = contextvars.ContextVar("fact_check_fc_results", default=None)


def set_report_context(raw_report_txt: str) -> None:
    _report_txt.set(raw_report_txt or "")


def prime_function_calls(raw_report_txt: str, calls) -> list:
    """함수 호출 배열 '전체'를 한 번의 LLM 호출로 사전 판단해 컨텍스트에 저장한다.

    이후 function_call 도구는 이 결과를 조회만 하므로, /invoke 1건당 함수 사용법 LLM 호출은 1회다.
    반환: [{call, valid, reason, confidence}, ...]  (실패 시 빈 목록으로 저장)
    """
    set_report_context(raw_report_txt)
    try:
        results = judge_function_calls(raw_report_txt, calls)  # ← 유일한 LLM 호출
    except Exception as exc:  # noqa: BLE001 - 사전 판단 실패가 전체를 막지 않도록
        logger.warning("function_call 사전 판단 실패: %s", exc)
        results = []
    _fc_results.set({r.get("call"): r for r in results if isinstance(r, dict) and r.get("call")})
    return results


def ensure_repo() -> None:
    """REPO_PATH가 비어 있고 REPO_URL이 설정돼 있으면 clone 한다(도커 편의용)."""
    if os.path.isdir(REPO_PATH) and os.listdir(REPO_PATH):
        return
    if REPO_URL:
        logger.info("REPO_PATH가 비어 있어 clone: %s → %s", REPO_URL, REPO_PATH)
        os.makedirs(REPO_PATH, exist_ok=True)
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


def git_history_query(ref: str) -> dict:
    """커밋 해시 또는 ref가 저장소 git 이력에 실제로 존재하는지 조회한다."""
    return get_engine().git_history_query(ref)


def header_lookup(name: str) -> dict:
    """헤더 파일이 존재하는지 조회한다.

    - 표준 C 라이브러리 헤더(stdio.h 등)는 존재로 본다(kind="standard_library").
    - POSIX/시스템 헤더(unistd.h, sys/socket.h 등)도 존재로 본다(kind="system").
    - 그 외에는 대상 저장소(REPO_PATH)에서 파일명으로 찾는다(kind="project" / "not_found").
    - 경로/괄호/따옴표가 붙어 와도 정규화한다: "<stdio.h>", "curl/curl.h", '"curl_printf.h"' 등.
    """
    raw = (name or "").strip()
    cleaned = raw.strip("<>\"' ")          # "<sys/socket.h>" → "sys/socket.h"
    base = Path(cleaned).name              # "curl/curl.h" → "curl.h"

    if base in STANDARD_HEADERS:
        return {"name": raw, "exists": True, "kind": "standard_library", "location": None}

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

    if cache is not None:
        if call in cache:
            return cache[call]
        return {"call": call, "valid": "UNKNOWN", "reason": "사전 판단 목록에 없는 호출", "confidence": 0.0}

    # prime이 호출되지 않은 경우(예: 도구 직접 사용)의 방어 — 단건 판단
    try:
        results = judge_function_calls(_report_txt.get(), [call])
    except Exception as exc:  # noqa: BLE001
        logger.warning("function_call LLM 판단 실패: %s", exc)
        return {"call": call, "valid": "UNKNOWN", "reason": f"판단 실패: {exc}", "confidence": 0.0}
    if results:
        r = dict(results[0])
        r.setdefault("call", call)
        return r
    return {"call": call, "valid": "UNKNOWN", "reason": "LLM이 결과를 반환하지 않음", "confidence": 0.0}
