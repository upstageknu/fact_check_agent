"""올바른 함수 사용법 판단 — 별도 LLM 호출.

fact_check_agent(도구를 오케스트레이션하는 LLM)와는 분리된, 새로 호출되는 LLM이다.
- 입력: 사용자가 제보한 보고서 원문(raw_report_txt) + 함수 호출 목록(function_calls 배열)
- 출력: 각 함수 호출마다 {call, valid, reason, confidence}
"""

import json
import logging

from agent import extract_json
from config import SOLAR_MODEL, get_client

logger = logging.getLogger("function_call_checker")

FUNCTION_CALL_CHECK_PROMPT = """너는 C 라이브러리 함수 사용법 검증 전문가다.

입력으로 버그 리포트 원문(raw_report_txt)과 함수 호출 목록(function_calls)이 JSON으로 주어진다.
각 함수 호출이 올바르게 사용되었는지(인자 개수·타입·순서, 그리고 보고서 맥락상 사용 방식)를 판단하라.

원칙:
- 보고서 원문 맥락과 함수 호출 문자열을 토대로 curl 공식 docs 등을 참고하여 판단한다. 필요에 따라 웹검색을 수행한다.
- 사실을 지어내지 않는다. 확실하지 않으면 판단하되 confidence를 낮춘다.
- reason에는 그렇게 판단한 구체적 근거를 한국어로 적는다.
- 
confidence는 0.0~1.0 사이 실수다.

출력 규칙:
- 반드시 JSON만 출력한다. 마크다운, 설명문, 코드펜스는 출력하지 않는다.
- 입력의 각 호출에 대해 정확히 하나의 결과 항목을 만든다(순서 유지).

출력 형식:
{
  "function_call_check": [
    {
      "call": "curl_mfprintf("%x %x %x %x", 10)",
      "valid": false,
      "reason": "curl_mfprintf (그리고 curl_mprintf 계열 함수)는 libcurl 내부에서 사용하는 래퍼 함수입니다.\n이 함수들은 포맷 문자열(format string) 을 첫 번째 인자로 받고, 그 뒤에 해당하는 인자들을 받아 처리하도록 만들어졌습니다.\n보고서에서 curl_mfprintf(stdout, user_input);처럼 사용자 입력을 format string 위치에 직접 넣는 코드를 테스트하고 있습니다.\n이는 프로그래머의 잘못된 사용이지, 라이브러리 자체의 취약점이 아닙니다. C 언어의 printf 계열 함수 모두에서 발생할 수 있는 일반적인 실수입니다.\ncurl 라이브러리 내부에서는 format string이 항상 상수 문자열 (literal)로 사용되거나, 제대로 된 인자와 함께 호출되므로 이 취약점이 실제로 발현되지 않습니다.",
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
        model=SOLAR_MODEL,
        messages=[
            {"role": "system", "content": FUNCTION_CALL_CHECK_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
    )
    data = extract_json(response.choices[0].message.content)
    return data.get("function_call_check", [])
