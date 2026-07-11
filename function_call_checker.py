"""올바른 함수 사용법 판단 — 별도 LLM 호출.

fact_check_agent(도구를 오케스트레이션하는 LLM)와는 분리된, 새로 호출되는 LLM이다.
입력:
- raw_report_txt: 버그 리포트 원문(리포터의 PoC/정의 코드 포함)
- function_calls: 검증할 함수 호출 문자열 목록
- known_signatures: 코드베이스에서 추출한 라이브러리 함수 실제 시그니처 맵({함수명: "시그니처"|"NOT_FOUND"})
- user_defined_functions: 리포터가 직접 정의한 함수명 목록(정의는 raw_report_txt 안)
출력: 각 호출마다 {call, valid(bool), reason, confidence}
  - valid는 true/false 두 값만. 판단 불가/근거 부족이면 true(무죄 추정).
"""

import json
import logging

from agent import extract_json
from config import SOLAR_MODEL, get_client

logger = logging.getLogger("function_call_checker")

FUNCTION_CALL_CHECK_PROMPT = """너는 C 라이브러리 함수 사용법 검증 전문가다.

입력(JSON):
- raw_report_txt: 버그 리포트 원문(리포터의 PoC/정의 코드 포함).
- function_calls: 검증할 함수 호출 문자열 목록.
- known_signatures: 대상 라이브러리 함수의 '실제 시그니처' 맵 {함수명: "시그니처" 또는 "NOT_FOUND"}.
  코드베이스에서 직접 추출한 사실이므로 최우선 근거로 삼는다.
- user_defined_functions: 리포터가 직접 정의한 함수명 목록. 이 함수들의 정의는 raw_report_txt 안에 있다.

각 함수 호출이 올바르게 사용되었는지(인자 개수·타입·순서, 그리고 보고서 맥락상 사용 방식)를 판단하라.

판단 근거:
- known_signatures에 실제 시그니처가 있으면 그것을 최우선으로 인자 개수/타입/순서를 검증한다.
- user_defined_functions의 함수는 raw_report_txt 안의 정의를 근거로 판단한다.
- 함수의 '존재 여부'는 판단 대상이 아니다(다른 단계가 처리한다). "시그니처를 못 찾음/선언이 안 보임"은
  잘못된 사용의 근거가 될 수 없다.
- 필요하면 curl 공식 docs 지식을 참고한다.

valid 규칙(매우 중요):
- valid는 반드시 boolean(true 또는 false) 하나로만 출력한다. "UNKNOWN"이나 null을 쓰지 않는다.
- valid=false는 '이 호출이 잘못 사용되었다'는 구체적 근거가 있을 때만 준다.
  (예: 알려진 시그니처와 인자 개수/타입/순서 불일치, 또는 format string에 사용자 입력을 직접 넣는 등 명백한 오용)
- 판단이 불확실하거나, 시그니처를 확인할 수 없거나, 근거가 부족하면 반드시 valid=true로 둔다(무죄 추정).

reason: 그렇게 판단한 구체적 근거를 한국어로 적는다.
confidence: 0.0~1.0 사이 실수.

출력 규칙:
- 반드시 JSON만 출력한다. 마크다운, 설명문, 코드펜스는 출력하지 않는다.
- 입력의 각 호출에 대해 정확히 하나의 결과 항목을 만든다(순서 유지).

출력 형식:
{
  "function_call_check": [
    {
      "call": "curl_mfprintf(user_input)",
      "valid": false,
      "reason": "사용자 입력을 format string 위치에 직접 넣는 명백한 오용(format string 취약 패턴).",
      "confidence": 0.85
    },
    {
      "call": "curl_url_cleanup(handle2)",
      "valid": true,
      "reason": "known_signatures에 CURLU* 인자 1개를 받는 시그니처가 있고 인자 개수가 일치함.",
      "confidence": 0.8
    }
  ]
}"""


def _coerce_valid_bool(items):
    """valid를 boolean으로 강제한다: 명시적 false만 false, 그 외(UNKNOWN/null/true) → true(무죄 추정)."""
    for it in items:
        if not isinstance(it, dict):
            continue
        v = it.get("valid")
        is_false = (v is False) or (isinstance(v, str) and v.strip().lower() == "false")
        it["valid"] = not is_false
    return items


def judge_function_calls(raw_report_txt: str, calls, signatures=None, user_defined_functions=None) -> list:
    """보고서 원문 + 함수 호출 배열 + 실제 시그니처를 받아, 각 호출의 올바른 사용 여부를 새 LLM으로 판단한다.

    반환: [{call, valid(bool), reason, confidence}, ...]  (실패 시 예외를 던짐)
    """
    calls = [c for c in (calls or []) if isinstance(c, str) and c.strip()]
    if not calls:
        return []
    payload = {
        "raw_report_txt": raw_report_txt or "",
        "function_calls": calls,
        "known_signatures": signatures or {},
        "user_defined_functions": user_defined_functions or [],
    }
    response = get_client().chat.completions.create(
        model=SOLAR_MODEL,
        messages=[
            {"role": "system", "content": FUNCTION_CALL_CHECK_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    data = extract_json(response.choices[0].message.content)
    items = _coerce_valid_bool(data.get("function_call_check", []))
    # call 문자열은 리포터 원문을 그대로 보존한다. LLM이 이스케이프 등을 정규화해 바꿔 써도,
    # '순서 유지·입력당 결과 1개' 규약에 따라 입력 순서에 맞춰 원본 문자열로 되돌린다.
    if len(items) == len(calls):
        for original, item in zip(calls, items):
            if isinstance(item, dict):
                item["call"] = original
    return items
