from __future__ import annotations

import json
from pathlib import Path

from .curl_scope import classify_curl_scope


def build_reporter_claim(record: dict) -> dict:
    source = record["source_record"]
    parser = record["parser"]
    return {
        "report_id": source.get("report_id"),
        "source_title": source.get("title"),
        "source_status": source.get("status"),
        "source_result": source.get("result"),
        "source_severity": source.get("severity"),
        "source_weakness": source.get("weakness"),
        "source_url": source.get("source_url"),
        "reporter_statement": {
            "title": parser.get("title") or source.get("title"),
            "vulnerability_type": parser.get("vuln_type"),
            "summary": parser.get("summary"),
            "affected_software": parser.get("affected_software"),
            "affected_version": parser.get("affected_version"),
            "affected_functions": parser.get("cited_functions") or [],
            "affected_headers": parser.get("cited_headers") or [],
            "function_calls": parser.get("function_calls") or [],
            "referenced_commits": parser.get("cited_commits") or [],
            "poc_present": bool(parser.get("poc_present")),
            "reproduction_steps": parser.get("repro_steps") or [],
            "claimed_impact": parser.get("claimed_impact"),
        },
        "curl_scope": classify_curl_scope(record),
        "expected_observation": infer_expected_observation(parser),
        "comparison_notes": [
            "This file captures the reporter's claim before any Docker execution.",
            "LLM judgement should compare Docker observations against this claim, not against final triage truth.",
        ],
    }


def infer_expected_observation(parser: dict) -> dict:
    summary = " ".join(
        str(value or "")
        for value in [
            parser.get("summary"),
            parser.get("claimed_impact"),
            " ".join(parser.get("repro_steps") or []),
        ]
    ).lower()

    expected = []
    crash_terms = ["crash", "segmentation fault", "segfault", "asan", "ubsan", "abort"]
    memory_terms = ["use-after-free", "buffer overflow", "out-of-bounds", "overread", "overflow", "underflow"]
    injection_terms = ["injection", "format string"]

    if any(term in summary for term in crash_terms):
        expected.append("crash_or_sanitizer_signal")
    if any(term in summary for term in memory_terms):
        expected.append("memory_safety_signal")
    if any(term in summary for term in injection_terms):
        expected.append("injection_or_untrusted_input_effect")
    if not expected:
        expected.append("unspecified_or_manual_review")

    return {
        "signals": expected,
        "raw_claimed_impact": parser.get("claimed_impact"),
    }


def write_claim(case_dir: Path, record: dict) -> dict:
    claim = build_reporter_claim(record)
    (case_dir / "claim.json").write_text(json.dumps(claim, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return claim
