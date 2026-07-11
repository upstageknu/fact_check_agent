from __future__ import annotations

import json
import re
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def safe_case_id(report_id: str) -> str:
    return f"hackerone_{re.sub(r'[^0-9A-Za-z_.-]+', '_', report_id)}"


def safe_json_loads(raw: str) -> dict:
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if match:
        text = match.group(1).strip()
    return json.loads(text)

