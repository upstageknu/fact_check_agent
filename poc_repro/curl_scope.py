from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parent / "data" / "curl_scope_catalog.json"


@lru_cache(maxsize=1)
def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def flatten_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    return str(value)


def collect_scope_text(record: dict) -> str:
    source = record.get("source_record", {})
    parser = record.get("parser", {})
    parts = [
        source.get("title"),
        source.get("weakness"),
        source.get("source_url"),
        parser.get("title"),
        parser.get("vuln_type"),
        parser.get("affected_software"),
        parser.get("summary"),
        parser.get("cited_functions"),
        parser.get("function_calls"),
        parser.get("cited_headers"),
        parser.get("cited_commits"),
        parser.get("poc_code"),
        parser.get("repro_steps"),
        parser.get("claimed_impact"),
    ]
    return flatten_text(parts)


def find_terms(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    matched = []
    for term in terms:
        needle = term.lower()
        if needle.startswith("--"):
            if re.search(rf"(?<!\w){re.escape(needle)}(?![\w-])", lowered):
                matched.append(term)
        elif needle.endswith("_"):
            if needle in lowered:
                matched.append(term)
        elif re.search(rf"(?<![a-z0-9_/-]){re.escape(needle)}(?![a-z0-9_/-])", lowered):
            matched.append(term)
    return sorted(set(matched))


def mentions_curl_project(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"(?<![a-z0-9_])curl(?![a-z0-9_])", lowered) or "libcurl" in lowered)


def classify_curl_scope(record: dict) -> dict:
    catalog = load_catalog()
    text = collect_scope_text(record)
    cli_matches = find_terms(text, catalog["curl_cli_terms"])
    libcurl_matches = find_terms(text, catalog["libcurl_terms"])
    non_scope_matches = find_terms(text, catalog["non_scope_terms"])

    if cli_matches and libcurl_matches:
        scope = "both"
        should_run = True
        reason = "Matched both curl command-line and libcurl indicators."
    elif libcurl_matches:
        scope = "libcurl"
        should_run = True
        reason = "Matched libcurl API/header/function indicators."
    elif cli_matches:
        scope = "curl_cli"
        should_run = True
        reason = "Matched curl command-line/tool indicators."
    elif mentions_curl_project(text) and not non_scope_matches:
        scope = "needs_scope_review"
        should_run = False
        reason = "Mentions curl but does not clearly identify curl command or libcurl."
    else:
        scope = "non_curl_reject"
        should_run = False
        reason = "Does not match curl command or libcurl scope indicators."

    return {
        "scope": scope,
        "should_run_in_docker": should_run,
        "reason": reason,
        "matched_curl_cli_terms": cli_matches,
        "matched_libcurl_terms": libcurl_matches,
        "matched_non_scope_terms": non_scope_matches,
        "catalog_schema_version": catalog["schema_version"],
    }

