from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .io_utils import load_jsonl, safe_case_id, safe_json_loads


DEFAULT_BASE_URL = "https://api.upstage.ai/v1"
DEFAULT_MODEL = "solar-pro3"
DEFAULT_TIMEOUT_SECONDS = 60.0

SYSTEM_PROMPT = """You are a PoC harness generation agent for defensive bug-bounty triage.

Your job is to convert a parsed vulnerability report into a minimal, local-only reproduction harness when possible.

Hard safety constraints:
- Generate only benign local reproduction code for a local container.
- Do not target public IPs, public domains, third-party services, or real credentials.
- Use loopback only if networking is required.
- Do not create persistence, privilege escalation, malware behavior, credential theft, scanners, worms, or destructive actions.
- Do not write outside the working directory.
- If the report needs a real vulnerable project checkout, external service, unavailable version, missing source tree, or an unsafe target, set can_generate=false.
- If the input is pseudocode, you may produce a best-effort harness, but mark assumptions clearly.
- Do not claim that a vulnerability is real. This is only for compile/run triage.

Return JSON only, with this schema:
{
  "can_generate": true,
  "language": "c|python|shell|none",
  "filename": "poc.c",
  "code": "...",
  "build_commands": ["cc -fsanitize=address,undefined -g -O0 poc.c -o poc -lcurl -lpthread"],
  "run_commands": ["./poc"],
  "expected_observation": "ASAN UAF, crash, timeout, or clean exit",
  "assumptions": ["..."],
  "limits": ["..."],
  "confidence": "low|medium|high"
}

If can_generate=false, set language="none", filename=null, code=null, and explain why in limits.
"""


def resolve_api_key(api_key: str | None = None, key_file: str | Path | None = None) -> str:
    if api_key:
        return api_key
    env_key = os.getenv("UPSTAGE_API_KEY")
    if env_key:
        return env_key
    if key_file:
        return Path(key_file).read_text(encoding="utf-8").strip()
    raise RuntimeError("UPSTAGE_API_KEY is required because LLM harness/judgement is enabled by default")


def create_llm_client(api_key: str | None, key_file: str | Path | None, base_url: str):
    """Create the shared PoC LLM client with a bounded request duration."""
    from openai import OpenAI

    timeout = float(os.getenv("POC_LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    return OpenAI(
        api_key=resolve_api_key(api_key, key_file),
        base_url=base_url,
        timeout=timeout,
        max_retries=1,
    )


def should_consider(record: dict) -> bool:
    parser = record["parser"]
    return bool(parser.get("poc_present") or parser.get("poc_code") or parser.get("repro_steps"))


def build_user_prompt(record: dict) -> str:
    source = record["source_record"]
    parser = record["parser"]
    compact = {
        "source_record": {
            "report_id": source.get("report_id"),
            "title": source.get("title"),
            "status": source.get("status"),
            "result": source.get("result"),
            "severity": source.get("severity"),
            "weakness": source.get("weakness"),
            "source_url": source.get("source_url"),
        },
        "parser": {
            "title": parser.get("title"),
            "vuln_type": parser.get("vuln_type"),
            "affected_software": parser.get("affected_software"),
            "affected_version": parser.get("affected_version"),
            "summary": parser.get("summary"),
            "cited_functions": parser.get("cited_functions"),
            "function_calls": parser.get("function_calls"),
            "cited_headers": parser.get("cited_headers"),
            "cited_commits": parser.get("cited_commits"),
            "poc_present": parser.get("poc_present"),
            "poc_code": parser.get("poc_code"),
            "repro_steps": parser.get("repro_steps"),
            "claimed_impact": parser.get("claimed_impact"),
        },
    }
    return (
        "Generate a safe local-only PoC harness candidate for this parsed report. "
        "If the report is invalid, vague, non-local, or cannot be reproduced without the real project tree, "
        "return can_generate=false.\n\n"
        + json.dumps(compact, ensure_ascii=False, indent=2)
    )


def case_dir_for(cases_dir: Path, report_id: str) -> Path:
    return cases_dir / safe_case_id(report_id)


def write_generation(case_dir: Path, generation: dict) -> None:
    llm_dir = case_dir / "llm"
    llm_dir.mkdir(parents=True, exist_ok=True)
    (llm_dir / "harness.json").write_text(json.dumps(generation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not generation.get("can_generate"):
        return

    filename = generation.get("filename")
    code = generation.get("code")
    if not filename or not code:
        return
    filename = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    (llm_dir / filename).write_text(code.rstrip() + "\n", encoding="utf-8")


def write_invalid_generation(case_dir: Path, report_id: str, title: str | None, raw_response: str, error: Exception) -> None:
    llm_dir = case_dir / "llm"
    llm_dir.mkdir(parents=True, exist_ok=True)
    (llm_dir / "response.txt").write_text(raw_response.rstrip() + "\n", encoding="utf-8")
    write_generation(
        case_dir,
        {
            "can_generate": False,
            "language": "none",
            "filename": None,
            "code": None,
            "build_commands": [],
            "run_commands": [],
            "expected_observation": "LLM response could not be parsed as JSON",
            "assumptions": [],
            "limits": ["LLM returned invalid JSON; inspect llm/response.txt or rerun with force_llm=True.", str(error)],
            "confidence": "low",
            "report_id": report_id,
            "source_title": title,
        },
    )


def select_records(
    parsed_path: Path,
    cases_dir: Path,
    report_ids: set[str],
    limit: int | None,
    generate: bool,
    force: bool,
) -> list[dict]:
    records = []
    for record in load_jsonl(parsed_path):
        report_id = record["source_record"]["report_id"]
        if report_ids and report_id not in report_ids:
            continue
        if should_consider(record):
            out = case_dir_for(cases_dir, report_id) / "llm" / "harness.json"
            if generate and out.exists() and not force:
                continue
            records.append(record)

    if limit is not None:
        records = records[:limit]
    return records


def print_selected(records: list[dict], cases_dir: Path) -> None:
    for record in records:
        source = record["source_record"]
        status = "exists" if (case_dir_for(cases_dir, source["report_id"]) / "llm" / "harness.json").exists() else "pending"
        print(source["report_id"], status, source["title"])


def generate_harnesses(
    records: list[dict],
    cases_dir: Path,
    api_key: str | None = None,
    key_file: str | Path | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    sleep_seconds: float = 0.2,
) -> int:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install package dependencies first: pip install -e .") from exc

    client = create_llm_client(api_key, key_file, base_url)
    generated = 0
    for index, record in enumerate(records, start=1):
        report_id = record["source_record"]["report_id"]
        print(f"[{index}/{len(records)}] generating harness for #{report_id}", flush=True)
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(record)},
                ],
            )
            raw_response = response.choices[0].message.content or "{}"
            generation = safe_json_loads(raw_response)
        except json.JSONDecodeError as exc:
            write_invalid_generation(
                case_dir_for(cases_dir, report_id),
                report_id,
                record["source_record"].get("title"),
                raw_response,
                exc,
            )
            print(f"  invalid JSON saved for manual review: {exc}", flush=True)
            continue
        except Exception as exc:
            write_invalid_generation(
                case_dir_for(cases_dir, report_id),
                report_id,
                record["source_record"].get("title"),
                "",
                exc,
            )
            print(f"  LLM harness failed; saved manual review stub: {type(exc).__name__}: {exc}", flush=True)
            continue
        generation["report_id"] = report_id
        generation["source_title"] = record["source_record"].get("title")
        write_generation(case_dir_for(cases_dir, report_id), generation)
        generated += 1
        if sleep_seconds:
            time.sleep(sleep_seconds)
    return generated
