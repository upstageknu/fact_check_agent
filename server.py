"""Fact-Check Agent HTTP API (FastAPI) — Active Pull 방식.

- POST /invoke : {report_id, trace_id, request_id}를 받아 오케스트레이터 DB에서 워크플로우
                 JSON을 조회하고, parser 결과를 실제 코드베이스와 결정론적으로 대조해
                 fact_check_result를 만든 뒤, 결과를 DB(invocations)에 등록하고 반환한다.
                 진행 중 각 단계는 이벤트 로그(events)로 남는다.
- GET  /health : 헬스체크 ({"status": "up" | "down"})

응답은 성공/에러 모두 {status_code, message, output} 봉투로 통일한다.

Swagger UI: /docs   ReDoc: /redoc   OpenAPI: /openapi.json
실행:  uvicorn server:app --host 0.0.0.0 --port 8000
"""

import json
import logging
import time
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import OpenAIError
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import ORCHESTRATOR_BASE_URL, REPO_PATH, SOLAR_MODEL, UPSTAGE_API_KEY
from runner import run_fact_check
from fact_check_tools import RepoError
from orchestrator import fetch_workflow, invocations_url, post_invocation
from tools import get_engine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("fact_check_api")

PROMPT_VERSION = "1.0"

app = FastAPI(
    title="Fact-Check Agent API",
    description=(
        "버그바운티 리포트의 함수/커밋/헤더/파일 인용과 호출 체인 도달성을 **실제 코드베이스·git 이력과 "
        "결정론적으로 대조**하는 Agent (Active Pull).\n\n"
        "`POST /invoke`에 `report_id`를 주면 오케스트레이터 DB에서 워크플로우 JSON을 조회하고, "
        "그 안의 `agent_results.parser`를 검증해 `fact_check_result`를 만든 뒤 DB에 등록한다.\n\n"
        "코드 근거는 결정론적으로 조회하며(ctags/git), Solar Pro 3가 도구 호출과 결과 요약을 담당한다. "
        "진행 중 각 단계는 events 로 실시간 기록되어 판정 근거를 추적할 수 있다(설명가능 AI).\n\n"
        f"- 조회: `GET {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{{report_id}}`\n"
        f"- 등록: `POST {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/{{report_id}}/agents/fact_check/invocations`\n"
        f"- 로그: `POST {ORCHESTRATOR_BASE_URL}/upstageknu2607/db/workflows/{{report_id}}/agents/fact_check/events`"
    ),
    version="1.0.0",
)


@app.on_event("startup")
def _startup() -> None:
    # 대상 저장소 인덱스를 미리 빌드해 첫 요청 지연을 줄인다(실패해도 서버는 뜬다).
    try:
        get_engine()
    except Exception:  # noqa: BLE001
        logger.exception("저장소 인덱싱 실패(첫 /invoke에서 재시도됨): REPO_PATH=%s", REPO_PATH)


# ---------------------------------------------------------------------------
# 스키마
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: Literal["ready", "down"]


class InvokeRequest(BaseModel):
    report_id: str = Field(..., description="검증 대상 리포트 ID. 이 ID로 오케스트레이터 DB를 조회한다.")
    trace_id: str = Field("", description="분산 추적 ID(응답/로그에 그대로 전달)")
    request_id: str = Field("", description="요청 ID(응답/로그에 그대로 전달)")
    agent_job_id: int | None = Field(None, description="오케스트레이터가 claim한 workflow_agent_jobs.id")

    model_config = {
        "json_schema_extra": {
            "example": {"report_id": "RPT-CURL-0001", "trace_id": "", "request_id": ""}
        }
    }


class InvokeOutput(BaseModel):
    report_id: str | None = None
    trace_id: str | None = None
    request_id: str | None = None
    fact_check: dict | None = None


class InvokeResponse(BaseModel):
    status_code: int = 200
    message: str = "fact_check completed"
    output: InvokeOutput


# ---------------------------------------------------------------------------
# 에러도 성공과 동일한 {status_code, message, output} 봉투로 반환
# ---------------------------------------------------------------------------

@app.exception_handler(StarletteHTTPException)
def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status_code": exc.status_code, "message": str(exc.detail), "output": None},
    )


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "status_code": 422,
            "message": "요청 형식이 올바르지 않습니다.",
            "output": jsonable_encoder(exc.errors()),
        },
    )


@app.exception_handler(Exception)
def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("처리되지 않은 오류 (%s)", request.url.path)
    return JSONResponse(
        status_code=500,
        content={"status_code": 500, "message": f"내부 오류: {exc}", "output": None},
    )


# ---------------------------------------------------------------------------
# 엔드포인트
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
    summary="헬스체크",
    description="서비스가 요청을 처리할 준비가 되었으면 `up`, 아니면(예: UPSTAGE_API_KEY 미설정) `down`.",
)
def health():
    return {"status": "ready" if UPSTAGE_API_KEY else "down"}


@app.post(
    "/invoke",
    response_model=InvokeResponse,
    tags=["fact_check"],
    summary="report_id로 워크플로우를 조회해 결정론적 사실 판단 수행 + DB 등록",
    description=(
        "요청 본문: `{\"report_id\": \"...\", \"trace_id\": \"...\", \"request_id\": \"...\"}`.\n\n"
        "1. `GET {BASE}/upstageknu2607/db/workflows/{report_id}` 로 워크플로우 JSON 조회\n"
        "2. `agent_results.parser`의 함수/커밋/헤더/파일/호출체인을 코드베이스와 결정론적으로 대조\n"
        "3. Solar Pro 3로 결과 요약(summary) 생성 (best-effort)\n"
        "4. 진행 단계는 `.../agents/fact_check/events` 로 실시간 로그, 최종 결과는 "
        "`.../agents/fact_check/invocations` 로 등록 (모두 best-effort)\n\n"
        "응답: `{\"status_code\": 200, \"message\": \"fact_check completed\", \"output\": {...}}`"
    ),
)
def invoke(req: InvokeRequest, rounds: int = Query(0, include_in_schema=False)):
    if not req.report_id:
        raise HTTPException(status_code=400, detail="report_id가 비어 있습니다.")

    # 1) 워크플로우 조회
    try:
        report = fetch_workflow(req.report_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"워크플로우 조회 실패(report_id={req.report_id}): HTTP {exc.response.status_code}",
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"오케스트레이터 연결 실패: {exc}")

    agent_results = report.get("agent_results") if isinstance(report, dict) else None
    if not isinstance(agent_results, dict):
        raise HTTPException(status_code=422, detail="조회한 워크플로우에 'agent_results' 객체가 없습니다.")
    parser_result = agent_results.get("parser")
    if not isinstance(parser_result, dict):
        raise HTTPException(status_code=422, detail="agent_results에 'parser' 결과가 없습니다.")

    raw_report_txt = (report.get("input") or {}).get("raw_report_txt", "") if isinstance(report.get("input"), dict) else ""

    # 2) Agent 도구 호출 검증
    started = time.perf_counter()
    try:
        fact_check_result = run_fact_check(
            parser_result,
            raw_report_txt=raw_report_txt,
            report_id=report.get("report_id") or req.report_id,
        )
    except RepoError as exc:
        raise HTTPException(status_code=500, detail=f"저장소 검증 오류: {exc}")
    except RuntimeError as exc:  # 예: UPSTAGE_API_KEY 미설정
        raise HTTPException(status_code=500, detail=str(exc))
    except OpenAIError as exc:  # 예: 잘못된 API 키, 모델명 오류, 레이트리밋 등
        raise HTTPException(status_code=502, detail=f"LLM 호출 실패: {exc}")
    duration_ms = int((time.perf_counter() - started) * 1000)

    output = {
        "report_id": report.get("report_id") or req.report_id,
        "trace_id": req.trace_id,
        "request_id": req.request_id,
        "fact_check": fact_check_result,
    }

    # 4) 최종 결과 DB 등록 (best-effort)
    _register_invocation(report, req, output, duration_ms)

    return {"status_code": 200, "message": "fact_check completed", "output": output}


def _register_invocation(report: dict, req: InvokeRequest, output: dict, duration_ms: int) -> None:
    """최종 결과를 오케스트레이터 invocations 엔드포인트로 POST한다(실패해도 응답은 정상)."""
    report_id = report.get("report_id") or req.report_id
    payload = {
        "agent_job_id": req.agent_job_id,
        "endpoint_url": REPO_PATH,
        "method": "POST",
        "request_payload": {
            "report_id": report_id,
            "trace_id": req.trace_id,
            "request_id": req.request_id,
        },
        "request_headers": {},
        "response_payload": output,
        "output": output["fact_check"],
        "error": {},
        "status_code": 200,
        "message": "fact_check completed",
        "status": "SUCCEEDED",
        "http_status": 200,
        "result_code": "OK",
        "result_message": "fact_check completed",
        "msg": "fact_check completed",
        "trace_id": req.trace_id,
        "request_id": req.request_id,
        "retry_count": 0,
        "timeout_seconds": 30,
        "duration_ms": duration_ms,
        "model": SOLAR_MODEL,
        "prompt_version": PROMPT_VERSION,
        "workflow_status": report.get("workflow_status") or "",
    }
    try:
        post_invocation(report_id, payload)
        logger.info("invocation 등록 완료: report_id=%s (%dms)", report_id, duration_ms)
    except httpx.HTTPError as exc:
        logger.warning("invocation 등록 실패(무시): %s → %s", invocations_url(report_id), exc)
