"""LLM Agent 도구 호출 루프 (사실판단_Agent_Gemini.ipynb의 run 패턴).

LLM(Grok)이 tool을 하나씩 호출하며 검증을 진행한다:
  chat.completions → tool_calls 있으면 실제 함수 실행 → 결과를 다시 전달 → 반복
  tool_calls가 없으면 최종 답변(JSON)으로 간주.
"""

import inspect
import json

from pydantic import BaseModel

from config import LLM_MODEL, get_client

MAX_STEPS = 24


class Agent(BaseModel):
    name: str = "Agent"
    model: str = LLM_MODEL
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
        response = client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        # 도구 호출이 없으면 최종 답변
        if not message.tool_calls:
            messages.append({"role": "assistant", "content": message.content})
            return message.content

        # 도구 호출(assistant) 메시지를 히스토리에 추가
        messages.append(message.model_dump(exclude_none=True))

        for tool_call in message.tool_calls:
            name = tool_call.function.name
            try:
                args = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = tool_map.get(name)
            print(f"  \U0001F527 [{agent.name}] {name}({args})")

            result = fn(**args) if fn else {"error": f"unknown tool: {name}"}

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })

    # 도구 루프가 한도에 도달하면, 도구 없이 최종 JSON만 요청한다.
    messages.append({"role": "user", "content": "이제 도구를 더 호출하지 말고 지금까지의 도구 결과만으로 최종 JSON만 출력하라."})
    response = client.chat.completions.create(
        model=agent.model,
        messages=[{"role": "system", "content": agent.instructions}] + messages,
    )
    return response.choices[0].message.content
