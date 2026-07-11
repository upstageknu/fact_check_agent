"""오케스트레이터 DB 연동 (fact_check Agent).

- fetch_workflow(report_id)   : 워크플로우 공통 JSON 조회 ("workflow" 래핑 벗김)
- post_invocation(report_id)  : 최종 결과 등록

경로:
  GET  {BASE}/upstageknu2607/db/workflows/{report_id}
  POST {BASE}/upstageknu2607/db/{report_id}/agents/fact_check/invocations
"""

import logging

import httpx

from config import ORCHESTRATOR_BASE_URL

logger = logging.getLogger("fact_check_orchestrator")

TIMEOUT = 30.0
AGENT = "fact_check"


def _base() -> str:
    return ORCHESTRATOR_BASE_URL.rstrip("/")


def workflow_url(report_id: str) -> str:
    return f"{_base()}/upstageknu2607/db/workflows/{report_id}"


def invocations_url(report_id: str) -> str:
    return f"{_base()}/upstageknu2607/db/workflows/{report_id}/agents/{AGENT}/invocations"


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
