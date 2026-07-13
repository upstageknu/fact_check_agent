"""오케스트레이터 DB 연동 (fact_check Agent).

- fetch_workflow(report_id)   : 워크플로우 공통 JSON 조회 ("workflow" 래핑 벗김)
- post_invocation(report_id)  : 최종 결과 등록

경로:
  GET  {BASE}/upstageknu2607/db/workflows/{report_id}
  POST {BASE}/upstageknu2607/db/workflows/{report_id}/agents/fact_check/invocations
  POST {BASE}/upstageknu2607/db/workflows/{report_id}/agents/fact_check/events
"""

import logging
import os

import httpx

from config import ORCHESTRATOR_BASE_URL

logger = logging.getLogger("fact_check_orchestrator")

TIMEOUT = 30.0
EVENT_TIMEOUT = float(os.getenv("WORKFLOW_EVENT_TIMEOUT_SECONDS", "2"))
AGENT = "fact_check"


def _base() -> str:
    return ORCHESTRATOR_BASE_URL.rstrip("/")


def workflow_url(report_id: str) -> str:
    return f"{_base()}/api/workflows/{report_id}"


def invocations_url(report_id: str) -> str:
    return f"{_base()}/api/workflows/{report_id}/agents/{AGENT}/invocations"


def events_url(report_id: str) -> str:
    return f"{_base()}/api/workflows/{report_id}/agents/{AGENT}/events"


def fetch_workflow(report_id: str) -> dict:
    """워크플로우 JSON을 조회해 안쪽 workflow dict를 반환한다(실패 시 httpx 예외를 던짐)."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.get(workflow_url(report_id))
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and isinstance(data.get("workflow"), dict):
        return data["workflow"]
    return data


def post_invocation(report_id: str, payload: dict) -> None:
    """최종 fact_check 결과 invocation을 등록한다(실패 시 httpx 예외를 던짐)."""
    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.post(invocations_url(report_id), json=payload)
    resp.raise_for_status()


def addLog(
    report_id: str,
    log: str,
    *,
    event_type: str = "log",
    level: str = "INFO",
    payload: dict | None = None,
    agent_name: str = AGENT,
    agent_job_id: int | None = None,
    trace_id: str = "",
    request_id: str = "",
    source: str = "agent",
) -> bool:
    """진행 중 결과를 이벤트 로그로 남긴다(설명가능 AI용).

    POST {BASE}/upstageknu2607/db/workflows/{report_id}/agents/fact_check/events

    best-effort: 로그 전송 실패가 서비스 흐름을 막지 않도록 예외를 던지지 않고 False를 반환한다.
    """
    if not report_id:
        return False
    body = {
        "event_type": event_type,
        "message": log,
        "payload": payload or {},
        "source": source,
        "level": level,
        "agent_name": agent_name,
        "agent_job_id": agent_job_id,
        "trace_id": trace_id,
        "request_id": request_id,
    }
    url = events_url(report_id)
    try:
        with httpx.Client(timeout=EVENT_TIMEOUT) as client:
            resp = client.post(url, json=body)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("addLog 실패(무시): %s → %s", url, exc)
        return False
