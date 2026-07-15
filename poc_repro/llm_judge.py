from __future__ import annotations

import json
import time
from pathlib import Path

from .io_utils import safe_case_id, safe_json_loads
from .llm_harness import DEFAULT_BASE_URL, DEFAULT_MODEL, create_llm_client


SYSTEM_PROMPT = """You are a defensive bug-bounty reproduction judge.

Your job is to compare:
1. The reporter's separated claim statement.
2. The local Docker compile/run result.

Decide whether the observed local execution supports the reporter's claimed result.

Rules:
- Do not decide whether the real product is vulnerable in the absolute.
- Judge only whether this local run matches the reporter's stated claim.
- If the PoC did not run, did not compile, timed out, or lacks the needed project/version/context, return inconclusive or not_supported.
- Sanitizer/crash output can support memory-safety claims.
- A clean run usually does not support crash/memory-corruption claims unless the claim expected a clean behavioral observation.
- Be conservative. Do not overclaim.

Return JSON only:
{
  "match": "confirmed|partially_supported|not_supported|inconclusive|not_executed",
  "claim_summary": "...",
  "observation_summary": "...",
  "matched_points": ["..."],
  "missing_points": ["..."],
  "contradictions": ["..."],
  "next_steps": ["..."],
  "confidence": "low|medium|high"
}
"""


def compact_command_result(item: dict) -> dict:
    return {
        "command": item.get("command"),
        "timed_out": item.get("timed_out"),
        "exit_code": item.get("exit_code"),
        "duration_seconds": item.get("duration_seconds"),
        "stdout_tail": (item.get("stdout") or "")[-4000:],
        "stderr_tail": (item.get("stderr") or "")[-4000:],
    }


def build_judge_prompt(claim: dict, result: dict) -> str:
    compact_result = {
        "report_id": result.get("report_id"),
        "title": result.get("title"),
        "kind": result.get("kind"),
        "verdict": result.get("verdict"),
        "skipped_reason": result.get("skipped_reason"),
        "llm_harness": result.get("llm_harness"),
        "compile": [compact_command_result(item) for item in result.get("compile", [])],
        "run": [compact_command_result(item) for item in result.get("run", [])],
    }
    payload = {
        "reporter_claim": claim,
        "docker_result": compact_result,
    }
    return "Compare the reporter claim with the Docker result.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def load_results(results_path: Path) -> list[dict]:
    if not results_path.exists():
        raise FileNotFoundError(f"results file not found: {results_path}")
    with results_path.open("r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_invalid_judgement(out: Path, report_id: str, raw_response: str, error: Exception) -> dict:
    judgement = {
        "report_id": report_id,
        "match": "inconclusive",
        "claim_summary": "",
        "observation_summary": "LLM response could not be parsed as JSON.",
        "matched_points": [],
        "missing_points": [],
        "contradictions": [],
        "next_steps": ["Inspect raw_response and rerun the judge."],
        "confidence": "low",
        "error": str(error),
        "raw_response": raw_response,
    }
    out.write(json.dumps(judgement, ensure_ascii=False) + "\n")
    return judgement


def judge_results(
    results_path: Path,
    cases_dir: Path,
    out_path: Path,
    *,
    api_key: str | None = None,
    key_file: str | Path | None = None,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    report_ids: list[str] | None = None,
    limit: int | None = None,
    sleep_seconds: float = 0.2,
) -> int:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("Install package dependencies first: pip install -e .") from exc

    selected_ids = set(report_ids or [])
    results = []
    for result in load_results(results_path):
        if selected_ids and result.get("report_id") not in selected_ids:
            continue
        results.append(result)
    if limit is not None:
        results = results[:limit]

    client = create_llm_client(api_key, key_file, base_url)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8") as out:
        for index, result in enumerate(results, start=1):
            report_id = result["report_id"]
            claim_path = cases_dir / safe_case_id(report_id) / "claim.json"
            if not claim_path.exists():
                judgement = {
                    "report_id": report_id,
                    "match": "inconclusive",
                    "claim_summary": "",
                    "observation_summary": "claim.json was not found for this case.",
                    "matched_points": [],
                    "missing_points": ["Missing separated reporter claim."],
                    "contradictions": [],
                    "next_steps": ["Regenerate cases so claim.json is created."],
                    "confidence": "low",
                }
                out.write(json.dumps(judgement, ensure_ascii=False) + "\n")
                count += 1
                continue

            print(f"[{index}/{len(results)}] judging claim match for #{report_id}", flush=True)
            claim = json.loads(claim_path.read_text(encoding="utf-8"))
            try:
                response = client.chat.completions.create(
                    model=model,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": build_judge_prompt(claim, result)},
                    ],
                )
                raw_response = response.choices[0].message.content or "{}"
                judgement = safe_json_loads(raw_response)
            except json.JSONDecodeError as exc:
                write_invalid_judgement(out, report_id, raw_response, exc)
                count += 1
                continue
            except Exception as exc:
                write_invalid_judgement(out, report_id, "", exc)
                count += 1
                continue

            judgement["report_id"] = report_id
            out.write(json.dumps(judgement, ensure_ascii=False) + "\n")
            count += 1
            if sleep_seconds:
                time.sleep(sleep_seconds)
    return count
