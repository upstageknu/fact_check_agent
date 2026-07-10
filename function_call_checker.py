"""올바른 함수 사용법 판단 — 별도 LLM 호출.

fact_check_agent(도구를 오케스트레이션하는 LLM)와는 분리된, 새로 호출되는 LLM이다.
- 입력: 사용자가 제보한 보고서 원문(raw_report_txt) + 함수 호출 목록(function_calls 배열)
- 출력: 각 함수 호출마다 {call, valid, reason, confidence}
"""

import json
import logging

from agent import extract_json
from config import LLM_MODEL, get_client

logger = logging.getLogger("function_call_checker")

FUNCTION_CALL_CHECK_PROMPT = """너는 C 라이브러리 함수 사용법 검증 전문가다.

입력으로 버그 리포트 원문(raw_report_txt)과 함수 호출 목록(function_calls)이 JSON으로 주어진다.
각 함수 호출이 올바르게 사용되었는지(인자 개수·타입·순서, 그리고 보고서 맥락상 사용 방식)를 판단하라.

원칙:
- 보고서 원문 맥락과 함수 호출 문자열을 근거로 판단한다.
- 사실을 지어내지 않는다. 확실하지 않으면 판단하되 confidence를 낮춘다.
- reason에는 그렇게 판단한 구체적 근거를 적는다.

confidence는 0.0~1.0 사이 실수다.

출력 규칙:
- 반드시 JSON만 출력한다. 마크다운, 설명문, 코드펜스는 출력하지 않는다.
- 입력의 각 호출에 대해 정확히 하나의 결과 항목을 만든다(순서 유지).

출력 형식:
{
  "function_call_check": [
    {
      "call": "curl_mfprintf(stdout, user_input)",
      "valid": true,
      "reason": "printf 계열 함수 호출로 인자 형태가 성립하며, 보고서의 사용 맥락과 일치함",
      "confidence": 0.8
    }
  ]
}"""


def judge_function_calls(raw_report_txt: str, calls) -> list:
    """보고서 원문과 함수 호출 배열을 받아, 각 호출의 올바른 사용 여부를 새 LLM으로 판단한다.

    반환: [{call, valid, reason, confidence}, ...]  (실패 시 예외를 던짐)
    """
    calls = [c for c in (calls or []) if isinstance(c, str) and c.strip()]
    if not calls:
        return []
    payload = {"raw_report_txt": raw_report_txt or "", "function_calls": calls}
    response = get_client().chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": FUNCTION_CALL_CHECK_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    data = extract_json(response.choices[0].message.content)
    return data.get("function_call_check", [])
