"""사실 판단 Agent 오케스트레이션.

Solar Pro 3가 도구(symbol_lookup / git_history_query / header_lookup / function_call)를
하나씩 호출하며 리포트의 claim을 실제 코드베이스와 대조하고, 최종 fact_check JSON을 만든다.

진행 중 모든 과정(시작/완료, 사전 판단, 각 도구 호출의 시작/종료)을 addLog 이벤트로 DB에 남긴다
(설명가능 AI).
"""

import json
import logging

from agent import Agent, extract_json, run
from config import SOLAR_MODEL
from orchestrator import addLog
from prompts import FACT_CHECK_AGENT_PROMPT
from tools import (
    function_call,
    get_engine,
    git_history_query,
    header_lookup,
    prime_function_calls,
    symbol_lookup,
)

logger = logging.getLogger("fact_check_runner")

# 함수 존재 여부(library_function_check)는 결정론적 사실이라 시스템이 symbol_lookup으로 직접 채운다.
# (에이전트 tools에서는 제외) 에이전트는 헤더/커밋/함수사용법만 담당한다.
fact_check_agent = Agent(
    name="Fact-check Agent",
    model=SOLAR_MODEL,
    instructions=FACT_CHECK_AGENT_PROMPT,
    tools=[git_history_query, header_lookup, function_call],
)


def build_claim(parser_result: dict) -> dict:
    """리포트 parser 결과에서 Agent가 검증할 claim을 뽑는다.

    함수 존재 판단은 라이브러리 함수(cited_library_functions)만 대상으로 한다.
    사용자 정의 함수(cited_user_defined_functions)는 존재 검증 대상이 아니다.
    """
    pr = parser_result or {}
    return {
        "title": pr.get("title"),
        "summary": pr.get("summary"),
        "cited_library_functions": pr.get("cited_library_functions", []) or [],
        "cited_headers": pr.get("cited_headers", []) or [],
        "cited_commits": pr.get("cited_commits", []) or [],
        "function_calls": pr.get("function_calls", []) or [],
        "poc_present": pr.get("poc_present"),
        "poc_code": pr.get("poc_code"),
    }


def _fallback_result(claim: dict, reason: str) -> dict:
    """LLM 최종 응답을 JSON으로 파싱하지 못했을 때의 안전 기본값(library_function_check는 이후 별도로 채움)."""
    return {
        "library_function_check": [],
        "header_check": [],
        "commit_check": [],
        "function_call_check": [],
        "poc_check": {"compilable": None, "compile_error": None},
        "summary": f"사실 판단 결과 파싱 실패: {reason}",
    }


def _check_library_functions(cited_library_functions, event_logger) -> list:
    """cited_library_functions의 각 함수명이 실제 존재하는지 symbol_lookup으로 확인한다(결정론).

    각 호출의 시작/종료를 이벤트로 남기고, [{name, exists, location}, ...]를 반환한다.
    """
    results = []
    for name in cited_library_functions:
        event_logger(
            "tool_start",
            f"[symbol_lookup] 호출 시작 name={name!r}",
            {"tool": "symbol_lookup", "args": {"name": name}, "phase": "start"},
        )
        r = symbol_lookup(name)
        event_logger(
            "tool_end",
            f"[symbol_lookup] 호출 종료 → exists={r.get('exists')}",
            {"tool": "symbol_lookup", "args": {"name": name}, "result": r, "phase": "end"},
        )
        results.append(r)
    return results


def run_fact_check(parser_result: dict, raw_report_txt: str = "", report_id: str = "", trace_id: str = "", request_id: str = "") -> dict:
    """parser 결과를 받아 Agent 도구 호출 루프로 검증하고 fact_check_result(dict)를 반환한다.

    raw_report_txt(보고서 원문)는 function_call 도구가 참조하는 별도 LLM 판단의 입력으로 쓰인다.
    """
    get_engine()  # 저장소 인덱스 준비(최초 1회)
    claim = build_claim(parser_result)

    def event_logger(event_type, message, payload=None):
        addLog(report_id, message, event_type=event_type, payload=payload,
               trace_id=trace_id, request_id=request_id)

    event_logger("fact_check", "사실 판단 시작", {"claim": claim})

    # 함수 호출 배열 '전체'를 한 번의 LLM 호출로 사전 판단(이후 function_call 도구는 조회만).
    event_logger("fact_check", "함수 사용법 사전 판단 시작(1회 LLM)", {"function_calls": claim["function_calls"]})
    fc_results = prime_function_calls(raw_report_txt, claim["function_calls"])
    event_logger("fact_check", "함수 사용법 사전 판단 완료", {"function_call_check": fc_results})

    messages = [{"role": "user", "content": json.dumps(claim, ensure_ascii=False, indent=2)}]
    final = run(messages, fact_check_agent, event_logger=event_logger)

    try:
        result = extract_json(final)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("최종 응답 JSON 파싱 실패: %s | raw[:200]=%r", exc, (final or "")[:200])
        result = _fallback_result(claim, str(exc))

    # 라이브러리 함수 존재 여부는 시스템이 symbol_lookup으로 결정론적으로 채운다(LLM 출력에 의존하지 않음).
    event_logger("fact_check", "라이브러리 함수 존재 검증 시작",
                 {"cited_library_functions": claim["cited_library_functions"]})
    result["library_function_check"] = _check_library_functions(claim["cited_library_functions"], event_logger)
    event_logger("fact_check", "라이브러리 함수 존재 검증 완료", {"library_function_check": result["library_function_check"]})

    event_logger("fact_check", "사실 판단 완료", {"summary": result.get("summary")})
    return result
