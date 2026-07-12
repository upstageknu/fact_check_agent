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
import re

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
- known_signatures에서 해당 함수 값이 "NOT_FOUND"이거나 맵에 아예 없으면, 너는 그 함수의 시그니처를
  절대 추측하거나 지어내지 마라. 시그니처가 없으므로 인자 개수/타입/순서 불일치 판정을 내릴 수 없고,
  반드시 valid=true(무죄 추정)로 둔다.
- known_signatures 값이 매크로(예: "#define foo(x)")이면 인자 '타입' 정보가 없다. 타입 불일치를 근거로
  valid=false를 주지 마라. 명백한 인자 '개수' 불일치가 아니면 valid=true(무죄 추정)로 둔다.
- 인자의 '변수명'만 보고 타입을 추측해 불일치로 판정하지 마라. 예: 호출이 f(buf, len, ...)이고 시그니처가
  f(struct X *a, int b, ...)일 때, buf/len이라는 이름만으로 타입이 다르다고 단정하면 안 된다.
  변수의 실제 타입은 raw_report_txt의 선언/문맥으로 '확인될 때만' 근거로 삼는다. 타입을 확인할 수 없으면
  타입 불일치로 valid=false를 주지 말고, 인자 '개수'가 일치하면 valid=true(무죄 추정)로 둔다.
- 필요하면 curl 공식 docs 지식을 참고한다.

valid 규칙(매우 중요):
- valid는 반드시 boolean(true 또는 false) 하나로만 출력한다. "UNKNOWN"이나 null을 쓰지 않는다.
- valid=false는 '이 호출이 잘못 사용되었다'는 구체적 근거가 있을 때만 준다.
  (예: 알려진 시그니처와 인자 '개수' 불일치, 또는 raw_report_txt에서 타입이 실제로 확인되어 시그니처와
  어긋나는 경우, 또는 format string에 사용자 입력을 직접 넣는 등 명백한 오용)
- 판단이 불확실하거나, 시그니처를 확인할 수 없거나, 인자 타입을 확인할 수 없거나, 근거가 부족하면
  반드시 valid=true로 둔다(무죄 추정).

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


def _unverifiable_functions(signatures) -> set:
    """인자 타입을 검증할 수 없는 함수명 집합.

    - NOT_FOUND: 코드베이스에서 시그니처 자체를 찾지 못함(이름 변경/삭제/외부 함수).
    - '#define ...' 매크로: 시그니처가 매크로 정의라 인자 '타입' 정보가 전혀 없다(인자 개수만 있음).
    두 경우 모두 인자 타입 불일치 판정의 근거가 될 수 없다.
    """
    out = set()
    for fn, sig in (signatures or {}).items():
        if not isinstance(sig, str):
            out.add(fn)
            continue
        s = sig.strip()
        if s.upper() == "NOT_FOUND" or s.startswith("#define"):
            out.add(fn)
    return out


def _guard_unverifiable(items, signatures) -> list:
    """시그니처를 검증할 수 없는 함수 호출은 valid=false를 낼 수 없다(결정론적 무죄 추정).

    시그니처가 NOT_FOUND이거나 매크로(#define)인 함수에 대해 '인자 타입/순서 불일치'로 판정하려면
    존재하지 않는 타입 정보에 의존할 수밖에 없다. LLM이 시그니처를 환각해 valid=false로 판단하더라도,
    여기서 valid=true로 되돌린다.
    """
    unverifiable = _unverifiable_functions(signatures)
    if not unverifiable:
        return items
    for it in items:
        if not isinstance(it, dict) or it.get("valid") is not False:
            continue
        call = it.get("call") or ""
        # 호출 문자열에서 '함수명(' 형태로 실제로 불린 검증 불가 함수를 찾는다.
        hit = next((fn for fn in unverifiable if re.search(r"\b" + re.escape(fn) + r"\s*\(", call)), None)
        if hit:
            it["valid"] = True
            it["reason"] = (
                f"'{hit}' 시그니처를 코드베이스에서 검증할 수 없어(미발견 또는 매크로 정의) "
                "인자 타입 불일치 판정 불가. 무죄 추정에 따라 valid=true로 처리함."
            )
    return items


# 최상위(괄호/대괄호/중괄호/따옴표 밖) 콤마로 인자를 나눈다.
def _split_top_level_args(inside: str):
    s = (inside or "").strip()
    if not s:
        return []
    args, depth, quote, cur = [], 0, None, []
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch; cur.append(ch); continue
        if ch in "([{":
            depth += 1; cur.append(ch); continue
        if ch in ")]}":
            depth -= 1; cur.append(ch); continue
        if ch == "," and depth == 0:
            args.append("".join(cur).strip()); cur = []; continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        args.append(tail)
    return args


def _paren_group(text: str, open_at: int):
    """text[open_at]가 '('일 때 짝이 맞는 ')'까지의 내부 문자열을 반환한다(못 찾으면 None)."""
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1:i]
    return None


def _call_arg_count(call: str, fn: str):
    """호출 문자열에서 fn(...)의 최상위 인자 개수. 파싱 불가면 None."""
    m = re.search(r"\b" + re.escape(fn) + r"\s*\(", call or "")
    if not m:
        return None
    inside = _paren_group(call, call.index("(", m.end() - 1))
    if inside is None:
        return None
    return len(_split_top_level_args(inside))


def _sig_param_count(sig: str):
    """시그니처 문자열의 파라미터 개수. void→0, 가변인자(...)나 파싱 불가면 None."""
    if not isinstance(sig, str):
        return None
    open_at = sig.find("(")
    if open_at < 0:
        return None
    inside = _paren_group(sig, open_at)
    if inside is None:
        return None
    params = _split_top_level_args(inside)
    if params == [] or (len(params) == 1 and params[0].replace("void", "").strip() == ""):
        return 0
    if any(p.strip() == "..." or p.strip().endswith("...") for p in params):
        return None  # 가변인자: 개수 비교가 무의미
    return len(params)


def _guard_no_verifiable_call(items, signatures) -> list:
    """검증 가능한 시그니처의 함수 호출이 문자열에 하나도 없으면 valid=false를 낼 수 없다.

    parser가 선언문/조건문 등 '함수 호출이 아닌' 코드 줄을 function_calls로 잘못 추출하면, 판정할
    함수 호출 자체가 없다. 이때 LLM이 false를 내더라도(근거 없음), 무죄 추정으로 valid=true로 되돌린다.
    검증 가능 = known_signatures에 실제 시그니처가 있는(NOT_FOUND/매크로 아닌) 함수.
    """
    sigs = signatures or {}
    verifiable = set(sigs) - _unverifiable_functions(sigs)
    for it in items:
        if not isinstance(it, dict) or it.get("valid") is not False:
            continue
        call = it.get("call") or ""
        if any(re.search(r"\b" + re.escape(fn) + r"\s*\(", call) for fn in verifiable):
            continue  # 검증 가능한 호출이 있으니 다른 가드/판정에 맡긴다
        it["valid"] = True
        it["reason"] = (
            "검증 가능한 시그니처의 함수 호출이 문자열에 없어(선언문/조건문 등 오추출로 추정) "
            "판정 대상이 없음. 무죄 추정에 따라 valid=true로 처리함."
        )
    return items


def _guard_elided_call(items, signatures) -> list:
    """호출 인자에 생략 표기 '...'가 있으면 실제 호출로 확정할 수 없어 valid=false를 낼 수 없다.

    C 함수 '호출'의 최상위 인자로 '...'가 오는 것은 문법적으로 불가능하다('...'는 선언부 가변인자
    표기). 따라서 호출 문자열에 '...'가 인자로 들어 있으면 parser가 원문을 잘라내거나 합성한 것이며,
    개수·타입 어느 것도 신뢰할 수 없다. 무죄 추정으로 valid=true로 되돌린다.
    """
    sigs = signatures or {}
    for it in items:
        if not isinstance(it, dict) or it.get("valid") is not False:
            continue
        call = it.get("call") or ""
        for fn in sigs:
            m = re.search(r"\b" + re.escape(fn) + r"\s*\(", call)
            if not m:
                continue
            inside = _paren_group(call, call.index("(", m.end() - 1))
            if inside is None:
                continue
            if any(a.strip() == "..." for a in _split_top_level_args(inside)):
                it["valid"] = True
                it["reason"] = (
                    f"'{fn}' 호출에 생략 표기(...)가 포함되어 실제 호출을 확정할 수 없음"
                    "(원문 잘림/합성 추정). 무죄 추정에 따라 valid=true로 처리함."
                )
            break
    return items


def _guard_type_mismatch_on_count_match(items, signatures) -> list:
    """실재하는 시그니처가 있고 인자 '개수'가 파라미터 개수와 일치하면, 타입/순서 불일치 기반
    valid=false를 무죄 추정으로 되돌린다.

    타입 불일치는 인자 '변수명'만으로는 확정할 수 없다(실제 타입은 리포터 코드 문맥에 있음).
    개수가 이미 일치하는데도 LLM이 이름만 보고 타입 불일치로 판정하는 오탐을 막는다.
    개수가 실제로 다른 경우는 건드리지 않으므로 진짜 '개수 오용'은 그대로 valid=false로 남는다.
    """
    sigs = signatures or {}
    for it in items:
        if not isinstance(it, dict) or it.get("valid") is not False:
            continue
        call = it.get("call") or ""
        for fn, sig in sigs.items():
            if not re.search(r"\b" + re.escape(fn) + r"\s*\(", call):
                continue
            pcount = _sig_param_count(sig)
            acount = _call_arg_count(call, fn)
            if pcount is None or acount is None or pcount != acount:
                continue
            it["valid"] = True
            it["reason"] = (
                f"'{fn}' 호출 인자 개수({acount})가 시그니처 파라미터 개수({pcount})와 일치함. "
                "타입 불일치는 인자 변수명만으로 확정할 수 없어(무죄 추정) valid=true로 처리함."
            )
            break
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
    # 시그니처를 검증할 수 없는(NOT_FOUND/매크로) 함수 호출은 LLM이 환각으로 valid=false를
    # 내더라도 무죄 추정으로 되돌린다.
    items = _guard_unverifiable(items, signatures)
    # 검증할 함수 호출 자체가 없는(선언문/조건문 오추출) 경우 무죄 추정으로 되돌린다.
    items = _guard_no_verifiable_call(items, signatures)
    # 인자에 생략 표기(...)가 있어 실제 호출로 확정 불가한 경우 무죄 추정으로 되돌린다.
    items = _guard_elided_call(items, signatures)
    # 인자 개수가 일치하는데 타입 불일치만으로 valid=false가 된 경우도 무죄 추정으로 되돌린다.
    items = _guard_type_mismatch_on_count_match(items, signatures)
    return items
