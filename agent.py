"""LLM Agent 도구 호출 루프 (사실판단_Agent_Gemini.ipynb의 run 패턴).

Solar Pro 3가 tool을 하나씩 호출하며 검증을 진행한다:
  chat.completions → tool_calls 있으면 실제 함수 실행 → 결과를 다시 전달 → 반복
  tool_calls가 없으면 최종 답변(JSON)으로 간주.
"""

import inspect
import json
import logging

from pydantic import BaseModel

from config import SOLAR_MODEL, get_client
from event_logger import timed_stage
from token_usage import add_response_usage

MAX_STEPS = 24
logger = logging.getLogger("fact_check_agent")


class Agent(BaseModel):
    name: str = "Agent"
    model: str = SOLAR_MODEL
    instructions: str = "You are a helpful Agent"
    tools: list = []


def function_to_schema(func) -> dict:
    """파이썬 함수를 LLM tool 스키마(JSON)로 변환한다 (시그니처 + docstring)."""
    type_map = {
        str: "string", int: "integer", float: "number",
        bool: "boolean", list: "array", dict: "object", type(None): "null",
    }
    signature = inspect.signature(func)
    parameters = {}
    for param in signature.parameters.values():
        param_type = type_map.get(param.annotation, "string")
        parameters[param.name] = {"type": param_type}
    required = [p.name for p in signature.parameters.values() if p.default is inspect._empty]
    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": (func.__doc__ or "").strip(),
            "parameters": {"type": "object", "properties": parameters, "required": required},
        },
    }


def extract_json(text: str) -> dict:
    """LLM 응답 문자열에서 JSON 객체만 추출해 dict로 변환한다."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def execute_tool_call(name: str, fn, raw_arguments: str | None) -> tuple[dict, dict]:
    """Validate and execute one LLM tool call without aborting the Agent loop.

    Models can violate the advertised JSON schema by omitting a required
    argument, adding an unknown argument, or emitting malformed JSON. Return
    those model errors as tool results so they cannot turn fact-check into 500.
    """
    try:
        args = json.loads(raw_arguments or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        return {}, {
            "error": {
                "type": "invalid_tool_arguments",
                "tool": name,
                "message": f"tool arguments are not valid JSON: {exc}",
            },
            "retryable": True,
        }

    if not isinstance(args, dict):
        return {}, {
            "error": {
                "type": "invalid_tool_arguments",
                "tool": name,
                "message": "tool arguments must be a JSON object",
            },
            "retryable": True,
        }

    if fn is None:
        return args, {
            "error": {
                "type": "unknown_tool",
                "tool": name,
                "message": f"unknown tool: {name}",
            },
            "retryable": False,
        }

    try:
        inspect.signature(fn).bind(**args)
    except TypeError as exc:
        required = [
            parameter.name
            for parameter in inspect.signature(fn).parameters.values()
            if parameter.default is inspect._empty
        ]
        return args, {
            "error": {
                "type": "invalid_tool_arguments",
                "tool": name,
                "message": str(exc),
                "required": required,
                "received": sorted(args),
            },
            "retryable": True,
            "instruction": "Call the tool again with all required arguments.",
        }

    try:
        return args, fn(**args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("tool execution failed: %s", name)
        return args, {
            "error": {
                "type": "tool_execution_error",
                "tool": name,
                "message": str(exc),
            },
            "retryable": False,
        }


def run(messages, agent, max_steps=MAX_STEPS):
    """도구 호출 루프를 돌려 최종 응답 문자열을 반환한다."""
    client = get_client()
    tool_schemas = [function_to_schema(t) for t in agent.tools]
    tool_map = {t.__name__: t for t in agent.tools}

    for _ in range(max_steps):
        kwargs = dict(
            model=agent.model,
            messages=[{"role": "system", "content": agent.instructions}] + messages,
        )
        if tool_schemas:
            kwargs["tools"] = tool_schemas
            kwargs["tool_choice"] = "auto"
        with timed_stage("llm_tool_call", payload={"agent": agent.name, "step": _ + 1}):
            response = client.chat.completions.create(**kwargs)
        add_response_usage(response)
        message = response.choices[0].message

        # 도구 호출이 없으면 최종 답변
        if not message.tool_calls:
            messages.append({"role": "assistant", "content": message.content})
            return message.content

        # 도구 호출(assistant) 메시지를 히스토리에 추가
        messages.append(message.model_dump(exclude_none=True))

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            fn = tool_map.get(name)
            with timed_stage("tool_execution", payload={"tool_name": name, "step": _ + 1}):
                args, result = execute_tool_call(
                    name, fn, tool_call.function.arguments
                )
            print(f"  \U0001F527 [{agent.name}] {name}({args})")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # 도구 루프가 한도에 도달하면, 도구 없이 최종 JSON만 요청한다.
    messages.append({"role": "user", "content": "이제 도구를 더 호출하지 말고 지금까지의 도구 결과만으로 최종 JSON만 출력하라."})
    with timed_stage("llm_final_response", payload={"agent": agent.name, "max_steps_reached": True}):
        response = client.chat.completions.create(
            model=agent.model,
            messages=[{"role": "system", "content": agent.instructions}] + messages,
        )
    add_response_usage(response)
    return response.choices[0].message.content
