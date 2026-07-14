import contextvars
import time
from contextlib import contextmanager

from orchestrator import addLog

_context = contextvars.ContextVar("fact_check_event_context", default={})


def configure_events(*, report_id: str, agent_job_id=None, trace_id=None, request_id=None) -> None:
    _context.set({
        "report_id": report_id,
        "agent_job_id": agent_job_id,
        "trace_id": trace_id or "",
        "request_id": request_id or "",
    })


def emit_event(event_type: str, message: str, *, payload=None, level: str = "INFO") -> bool:
    context = _context.get() or {}
    report_id = context.get("report_id")
    if not report_id:
        return False
    return addLog(
        report_id,
        message,
        event_type=event_type,
        level=level,
        payload=payload or {},
        agent_job_id=context.get("agent_job_id"),
        trace_id=context.get("trace_id", ""),
        request_id=context.get("request_id", ""),
    )


@contextmanager
def timed_stage(stage_name: str, *, payload=None, event_prefix: str = "fact_check.stage"):
    base_payload = {"stage": stage_name, **(payload or {})}
    started = time.perf_counter()
    emit_event(f"{event_prefix}.started", f"{stage_name} started", payload=base_payload)
    try:
        yield
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        emit_event(
            f"{event_prefix}.failed",
            f"{stage_name} failed after {duration_ms}ms",
            payload={**base_payload, "duration_ms": duration_ms, "error_type": type(exc).__name__, "error": str(exc)[:500]},
            level="ERROR",
        )
        raise
    else:
        duration_ms = int((time.perf_counter() - started) * 1000)
        emit_event(
            f"{event_prefix}.completed",
            f"{stage_name} completed in {duration_ms}ms",
            payload={**base_payload, "duration_ms": duration_ms},
        )
