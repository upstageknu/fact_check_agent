from contextvars import ContextVar

_usage = ContextVar("token_usage", default=None)


def reset_usage() -> None:
    _usage.set({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0})


def add_response_usage(response) -> None:
    current = dict(_usage.get() or {})
    usage = getattr(response, "usage", None)
    current["prompt_tokens"] = current.get("prompt_tokens", 0) + int(getattr(usage, "prompt_tokens", 0) or 0)
    current["completion_tokens"] = current.get("completion_tokens", 0) + int(getattr(usage, "completion_tokens", 0) or 0)
    current["total_tokens"] = current.get("total_tokens", 0) + int(getattr(usage, "total_tokens", 0) or 0)
    current["llm_calls"] = current.get("llm_calls", 0) + 1
    _usage.set(current)


def get_usage() -> dict:
    return dict(_usage.get() or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_calls": 0})
