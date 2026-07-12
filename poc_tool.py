"""PoC 재현 도구 — fact_check Agent가 호출하는 도구.

내부에 복사해 둔 poc_repro 패키지(pipeline.run_pipeline)를 그대로 사용해,
리포트의 PoC 코드를 Docker에서 컴파일/실행하고 reporter claim과 대조한 결과를 돌려준다.
poc_repro의 구현은 수정하지 않고, 여기서 입력(JSONL) 변환과 결과 요약만 담당한다.

function_call 도구와 동일하게, 요청별 입력(report_id/parser/원문)은 ContextVar로 주입한다.
"""

import contextvars
import json
import logging
import tempfile
from pathlib import Path

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
from poc_repro.io_utils import safe_case_id
from poc_repro.pipeline import DEFAULT_IMAGE, run_pipeline

logger = logging.getLogger("fact_check_poc_tool")


def load_result_lines(path: Path) -> list:
    """결과 JSONL을 읽는다(파일이 없으면 빈 목록)."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

# 요청별 격리를 위한 컨텍스트(현재 검증 중인 리포트의 PoC 입력).
_poc_ctx = contextvars.ContextVar("fact_check_poc_ctx", default=None)
# poc_reproduce가 반환한 '구조화된 결과'를 담는다. 시스템이 이 값을 poc_check 슬롯에
# 결정론적으로 채우기 위해 사용한다(LLM이 최종 JSON에 전사하는 데 의존하지 않음).
_poc_result = contextvars.ContextVar("fact_check_poc_result", default=None)


def prime_poc_context(report_id: str, parser_result: dict, raw_report_txt: str = "") -> None:
    """이번 /invoke 요청에서 재현할 PoC 입력을 컨텍스트에 저장한다(run 전에 호출)."""
    _poc_ctx.set(
        {
            "report_id": report_id or "RPT-FACTCHECK",
            "parser": parser_result or {},
            "raw_report_txt": raw_report_txt or "",
        }
    )
    # 스레드 재사용(ThreadPoolExecutor) 시 이전 리포트 결과가 새 나가지 않도록 초기화한다.
    _poc_result.set(None)


def get_poc_result():
    """이번 요청에서 poc_reproduce가 만든 구조화된 결과를 반환한다(호출 안 됐으면 None)."""
    return _poc_result.get()


def _build_record(ctx: dict) -> dict:
    """poc_repro가 기대하는 parsed_reports.jsonl 한 줄({source_record, parser})을 만든다."""
    parser = ctx["parser"]
    return {
        "source_record": {
            "report_id": ctx["report_id"],
            "title": parser.get("title"),
            "status": None,
            "result": None,
            "weakness": parser.get("vuln_type"),
            "source_url": None,
        },
        "parser": parser,
    }


def _summarize(report_id: str, result, results_dir: Path) -> dict:
    """results.jsonl / judgements.jsonl에서 이 리포트 결과만 뽑아 compact dict로 요약한다."""
    case_id = safe_case_id(report_id)
    run_rec = None
    for rec in load_result_lines(results_dir / "results.jsonl"):
        if rec.get("report_id") in (report_id, case_id.replace("hackerone_", "")):
            run_rec = rec
            break
    judge_rec = None
    for rec in load_result_lines(results_dir / "judgements.jsonl"):
        if rec.get("report_id") in (report_id, case_id.replace("hackerone_", "")):
            judge_rec = rec
            break

    if run_rec is None:
        return {
            "reproduced": False,
            "verdict": "NOT_EXECUTED",
            "compilable": None,
            "compile_error": None,
            "skipped_reason": "PoC 후보가 없거나 실행 대상으로 분류되지 않음",
            "judgement": (judge_rec or {}).get("match"),
        }

    compiles = run_rec.get("compile") or []
    runs = run_rec.get("run") or []
    compilable = None
    compile_error = None
    if compiles:
        ok = any(c.get("exit_code") == 0 for c in compiles)
        compilable = ok
        if not ok:
            compile_error = ((compiles[-1].get("stderr") or "")[-2000:]) or None

    return {
        "reproduced": run_rec.get("verdict") in {"REPRO_LIKELY", "NONZERO_EXIT", "RAN_CLEAN"},
        "verdict": run_rec.get("verdict"),
        "compilable": compilable,
        "compile_error": compile_error,
        "skipped_reason": run_rec.get("skipped_reason"),
        "run_stdout_tail": ((runs[-1].get("stdout") or "")[-1500:]) if runs else None,
        "run_stderr_tail": ((runs[-1].get("stderr") or "")[-1500:]) if runs else None,
        "judgement": (judge_rec or {}).get("match"),
        "judgement_summary": (judge_rec or {}).get("observation_summary"),
    }


def poc_reproduce() -> dict:
    """리포트의 PoC 코드를 Docker 샌드박스에서 컴파일/실행해 reporter의 주장과 대조한다.

    반환: {reproduced, verdict, compilable, compile_error, skipped_reason, judgement, ...}
    verdict 값: REPRO_LIKELY(크래시/재현 유력), NONZERO_EXIT, RAN_CLEAN(정상 종료),
    COMPILE_FAILED, TIMEOUT, NEEDS_MANUAL_REVIEW, OUT_OF_SCOPE_REJECT, NOT_EXECUTED.
    Docker 미실행 등으로 재현 불가 시 verdict=NOT_EXECUTED와 error를 돌려준다(무죄추정).

    결과는 _poc_result 컨텍스트에도 저장돼, 시스템이 poc_check 슬롯을 결정론적으로 채운다.
    """
    out = _reproduce()
    _poc_result.set(out)
    return out


def _reproduce() -> dict:
    ctx = _poc_ctx.get()
    if not ctx:
        return {"reproduced": False, "verdict": "NOT_EXECUTED",
                "compilable": None, "compile_error": None,
                "skipped_reason": "PoC 컨텍스트가 초기화되지 않음"}

    record = _build_record(ctx)
    report_id = ctx["report_id"]

    try:
        with tempfile.TemporaryDirectory(prefix="poc_repro_") as tmp:
            work_dir = Path(tmp)
            parsed_jsonl = work_dir / "parsed_reports.jsonl"
            parsed_jsonl.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

            result = run_pipeline(
                parsed_jsonl=parsed_jsonl,
                work_dir=work_dir,
                report_ids=[report_id],
                clean=True,
                with_llm=True,
                judge_with_llm=True,
                upstage_api_key=LLM_API_KEY,
                upstage_base_url=LLM_BASE_URL,
                model=LLM_MODEL,
                image=DEFAULT_IMAGE,
                build_docker=True,
                run_docker=True,
            )
            return _summarize(report_id, result, result.results_dir)
    except Exception as exc:  # noqa: BLE001 - 재현 실패가 fact_check 전체를 막지 않도록
        logger.warning("poc_reproduce 실패(무시): %s", exc)
        return {"reproduced": False, "verdict": "NOT_EXECUTED",
                "compilable": None, "compile_error": None,
                "skipped_reason": f"PoC 재현 실행 실패: {exc}"}
