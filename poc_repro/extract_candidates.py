from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from .claims import write_claim
from .curl_scope import classify_curl_scope
from .io_utils import load_jsonl, safe_case_id


def is_placeholder(code: str) -> bool:
    compact = re.sub(r"\s+", " ", code).strip()
    return "..." in compact or compact in {"int main() { }", "int main() { ... }"}


def looks_like_c(code: str) -> bool:
    return bool(
        re.search(r"(?m)^\s*#\s*include\b", code)
        or re.search(r"(?m)^\s*include\s*[<\"]", code)
        or re.search(r"\bint\s+main\s*\(", code)
        or re.search(r"\bcurl_easy_", code)
    )


def looks_like_shell(code: str) -> bool:
    return bool(
        re.search(r"(?m)^\s*(curl|gcc|clang|python3?|bash|sh|sudo|docker|make)\b", code)
        or re.search(r"\|\s*(bash|sh)\b", code)
    )


def strip_hackerone_line_numbers(code: str) -> str:
    cleaned = []
    for line in code.splitlines():
        stripped = line.strip()
        if stripped in {"Code", "•"}:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?\s*(?:B|KiB|MiB|Bytes)", stripped, re.I):
            continue
        line = re.sub(r"^\s*\d+(?=(#|include\b|[A-Za-z_/{]|$))", "", line)
        cleaned.append(line)
    return "\n".join(cleaned)


def trim_after_main_block(code: str) -> tuple[str, bool]:
    main_match = re.search(r"\bint\s+main\s*\([^)]*\)", code)
    if not main_match:
        return code, False
    brace_start = code.find("{", main_match.end())
    if brace_start == -1:
        return code, False

    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(brace_start, len(code)):
        char = code[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == in_string:
                in_string = None
            continue
        if char in {'"', "'"}:
            in_string = char
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                tail = code[index + 1 :].strip()
                if tail:
                    return code[: index + 1], True
                return code, False
    return code, False


def normalize_c_code(code: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    code = strip_hackerone_line_numbers(code)

    def include_repl(match: re.Match) -> str:
        notes.append("converted bare include to #include")
        return f"{match.group(1)}#include {match.group(2)}"

    code = re.sub(r"(?m)^(\s*)include\s+([<\"].*)$", include_repl, code)

    def slash_comment_repl(match: re.Match) -> str:
        notes.append("converted slash-delimited pseudo comment to C block comment")
        return f"{match.group(1)}/* {match.group(2).strip()} */"

    code = re.sub(r"(?m)^(\s*)/\s*([^/\n][^/\n]*?)\s*/\s*$", slash_comment_repl, code)

    if "#include" not in code:
        inferred = []
        if re.search(r"\bprintf\s*\(", code):
            inferred.append("#include <stdio.h>")
        if re.search(r"\bcurl_", code) or "CURL" in code:
            inferred.append("#include <curl/curl.h>")
        if inferred:
            notes.append("added inferred standard includes")
            code = "\n".join(inferred) + "\n" + code

    code, trimmed = trim_after_main_block(code)
    if trimmed:
        notes.append("trimmed non-C text after main() block")

    return code.strip() + "\n", notes


def classify_candidate(record: dict) -> dict:
    parser = record["parser"]
    source = record["source_record"]
    code = parser.get("poc_code") or ""
    steps = parser.get("repro_steps") or []

    candidate = {
        "report_id": source["report_id"],
        "title": source.get("title"),
        "status": source.get("status"),
        "result": source.get("result"),
        "weakness": source.get("weakness"),
        "source_url": source.get("source_url"),
        "poc_present": bool(parser.get("poc_present")),
        "has_poc_code": bool(code),
        "has_repro_steps": bool(steps),
        "kind": "manual",
        "runnable": False,
        "reason": None,
        "normalization_notes": [],
    }
    candidate["curl_scope"] = classify_curl_scope(record)

    if not code:
        candidate["reason"] = "no parser.poc_code"
        return candidate
    if is_placeholder(code):
        candidate["reason"] = "placeholder or incomplete code"
        return candidate
    if looks_like_c(code):
        normalized, notes = normalize_c_code(code)
        candidate.update(
            {
                "kind": "c",
                "runnable": bool(candidate["curl_scope"].get("should_run_in_docker")),
                "reason": (
                    "C-like PoC candidate"
                    if candidate["curl_scope"].get("should_run_in_docker")
                    else f"C-like PoC candidate blocked by scope gate: {candidate['curl_scope'].get('scope')}"
                ),
                "normalized_code": normalized,
                "normalization_notes": notes,
            }
        )
        return candidate
    if looks_like_shell(code):
        default_runnable = candidate["curl_scope"].get("scope") in {"curl_cli", "both"}
        candidate.update(
            {
                "kind": "shell",
                "runnable": default_runnable,
                "reason": (
                    "curl CLI command candidate; enabled by default in network-isolated Docker"
                    if default_runnable
                    else "shell-like reproduction steps; disabled by default"
                ),
                "normalized_code": code.strip() + "\n",
            }
        )
        return candidate

    candidate["reason"] = "poc_code does not look directly executable"
    return candidate


def write_case(out_dir: Path, candidate: dict, source_record: dict) -> None:
    case_dir = out_dir / safe_case_id(candidate["report_id"])
    case_dir.mkdir(parents=True, exist_ok=True)
    metadata = {key: value for key, value in candidate.items() if key != "normalized_code"}
    metadata["source_record"] = source_record["source_record"]
    metadata["parser_summary"] = {
        "affected_software": source_record["parser"].get("affected_software"),
        "affected_version": source_record["parser"].get("affected_version"),
        "cited_functions": source_record["parser"].get("cited_functions"),
        "cited_headers": source_record["parser"].get("cited_headers"),
        "claimed_impact": source_record["parser"].get("claimed_impact"),
        "repro_steps": source_record["parser"].get("repro_steps"),
    }
    (case_dir / "case.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if candidate.get("kind") == "c" and candidate.get("normalized_code"):
        (case_dir / "poc.c").write_text(candidate["normalized_code"], encoding="utf-8")
    elif candidate.get("kind") == "shell" and candidate.get("normalized_code"):
        (case_dir / "run.sh").write_text(candidate["normalized_code"], encoding="utf-8")
    write_claim(case_dir, source_record)


def remove_tree(path: Path) -> None:
    def handle_remove_error(func, failed_path, exc_info) -> None:
        try:
            os.chmod(failed_path, stat.S_IWRITE)
            func(failed_path)
        except Exception:
            raise exc_info[1]

    shutil.rmtree(path, onerror=handle_remove_error)


def extract_candidates(parsed_path: Path, out_dir: Path, clean: bool = False) -> dict:
    if clean and out_dir.exists():
        remove_tree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    records = load_jsonl(parsed_path)
    candidates = []
    for record in records:
        candidate = classify_candidate(record)
        if candidate["poc_present"] or candidate["has_poc_code"] or candidate["has_repro_steps"]:
            candidates.append(candidate)
            write_case(out_dir, candidate, record)

    manifest = {
        "source": str(parsed_path),
        "out_dir": str(out_dir),
        "total_parsed_reports": len(records),
        "candidate_count": len(candidates),
        "runnable_count": sum(1 for item in candidates if item["runnable"]),
        "manual_count": sum(1 for item in candidates if not item["runnable"]),
        "candidates": [{key: value for key, value in item.items() if key != "normalized_code"} for item in candidates],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest
