"""사실 판단 Agent 오케스트레이션.

LLM(Grok)이 도구(symbol_lookup / git_history_query / header_lookup / function_call)를
하나씩 호출하며 리포트의 claim을 실제 코드베이스와 대조하고, 최종 fact_check JSON을 만든다.
"""

import json
import logging

from agent import Agent, extract_json, run
from config import LLM_MODEL
from prompts import FACT_CHECK_AGENT_PROMPT
from tools import (
    function_call,
    get_engine,
    git_history_query,
    header_lookup,
    prime_function_calls,
    signature_lookup,
    symbol_lookup,
)

logger = logging.getLogger("fact_check_runner")

# 함수 존재 여부(library_function_check)는 결정론적 사실이라 시스템이 symbol_lookup으로 직접 채운다.
# (에이전트 tools에서는 제외) 에이전트는 헤더/커밋/함수사용법만 담당한다.
fact_check_agent = Agent(
    name="Fact-check Agent",
    model=LLM_MODEL,
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
        "cited_user_defined_functions": pr.get("cited_user_defined_functions", []) or [],
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


def _check_library_functions(cited_library_functions) -> list:
    """cited_library_functions의 각 함수명이 실제 존재하는지 symbol_lookup으로 확인한다(결정론).

    [{name, exists, location}, ...]를 반환한다.
    """
    return [symbol_lookup(name) for name in cited_library_functions]


def run_fact_check(parser_result: dict, raw_report_txt: str = "") -> dict:
    """parser 결과를 받아 Agent 도구 호출 루프로 검증하고 fact_check_result(dict)를 반환한다.

    raw_report_txt(보고서 원문)는 function_call 도구가 참조하는 별도 LLM 판단의 입력으로 쓰인다.
    """
    get_engine()  # 저장소 인덱스 준비(최초 1회)
    claim = build_claim(parser_result)

    # 함수 호출 배열 '전체'를 한 번의 LLM 호출로 사전 판단(이후 function_call 도구는 조회만).
    # 라이브러리 함수의 '실제 시그니처'를 코드베이스에서 추출해 판단 근거로 함께 제공한다.
    known_signatures = {
        fn: (signature_lookup(fn) or "NOT_FOUND") for fn in claim["cited_library_functions"]
    }
    prime_function_calls(
        raw_report_txt, claim["function_calls"],
        signatures=known_signatures,
        user_defined_functions=claim["cited_user_defined_functions"],
    )

    messages = [{"role": "user", "content": json.dumps(claim, ensure_ascii=False, indent=2)}]
    final = run(messages, fact_check_agent)

    try:
        result = extract_json(final)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("최종 응답 JSON 파싱 실패: %s | raw[:200]=%r", exc, (final or "")[:200])
        result = _fallback_result(claim, str(exc))

    # 라이브러리 함수 존재 여부는 시스템이 symbol_lookup으로 결정론적으로 채운다(LLM 출력에 의존하지 않음).
    result["library_function_check"] = _check_library_functions(claim["cited_library_functions"])

    return result
