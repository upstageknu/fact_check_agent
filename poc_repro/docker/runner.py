from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path


CRASH_PATTERNS = [
    "AddressSanitizer",
    "UndefinedBehaviorSanitizer",
    "heap-use-after-free",
    "stack-buffer-overflow",
    "heap-buffer-overflow",
    "global-buffer-overflow",
    "runtime error:",
    "Segmentation fault",
    "SIGSEGV",
    "SIGABRT",
    "double-free",
    "out-of-bounds",
    "use-after-free",
]

CURL_CLI_SCOPES = {"curl_cli", "both"}
CURL_CLI_BINARIES = {"curl", "curl.exe", "wcurl"}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_scope_gate(case_dir: Path) -> dict | None:
    claim_path = case_dir / "claim.json"
    if not claim_path.exists():
        return None
    claim = load_json(claim_path)
    return claim.get("curl_scope")


def detect_curl_environment(requested: dict) -> dict:
    environment = dict(requested)
    probes = {
        "actual_curl_version": ["curl", "--version"],
        "actual_libcurl_version": ["curl-config", "--version"],
    }
    for field, command in probes.items():
        try:
            completed = subprocess.run(command, text=True, capture_output=True, timeout=5, check=False)
            match = re.search(r"\b(\d+\.\d+\.\d+)\b", completed.stdout)
            environment[field] = match.group(1) if completed.returncode == 0 and match else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            environment[field] = None

    requested_version = environment.get("requested_curl_version")
    if requested_version:
        exact = all(environment.get(field) == requested_version for field in ("actual_curl_version", "actual_libcurl_version"))
        if exact:
            environment["match_status"] = (
                "ASSUMED_LATEST"
                if environment.get("match_status") == "ASSUMED_LATEST"
                else "EXACT"
            )
        else:
            environment["match_status"] = "VERSION_MISMATCH"
        environment["allow_execution"] = bool(environment.get("allow_execution")) and exact
    return environment


def run_command(command: list[str] | str, cwd: Path, timeout: int, shell: bool = False) -> dict:
    started = time.time()
    try:
        completed = subprocess.run(
            command if shell else list(command),
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            shell=shell,
            env={
                **os.environ,
                "ASAN_OPTIONS": "detect_leaks=0:halt_on_error=0:abort_on_error=0",
                "UBSAN_OPTIONS": "halt_on_error=0:print_stacktrace=1",
            },
        )
        return {
            "command": command if isinstance(command, str) else " ".join(shlex.quote(part) for part in command),
            "timeout_seconds": timeout,
            "timed_out": False,
            "exit_code": completed.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-12000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command if isinstance(command, str) else " ".join(shlex.quote(part) for part in command),
            "timeout_seconds": timeout,
            "timed_out": True,
            "exit_code": None,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-12000:] if isinstance(exc.stderr, str) else "",
        }


def classify(run_results: list[dict], compile_results: list[dict], skipped_reason: str | None = None) -> str:
    if skipped_reason:
        return "NEEDS_MANUAL_REVIEW"
    if compile_results and all(item["exit_code"] not in (0, None) for item in compile_results):
        return "COMPILE_FAILED"
    if any(item.get("timed_out") for item in run_results):
        return "TIMEOUT"

    combined = "\n".join((item.get("stdout") or "") + "\n" + (item.get("stderr") or "") for item in run_results)
    if any(pattern.lower() in combined.lower() for pattern in CRASH_PATTERNS):
        return "REPRO_LIKELY"
    if any(item.get("exit_code") not in (0, None) for item in run_results):
        return "NONZERO_EXIT"
    if run_results:
        return "RAN_CLEAN"
    return "NEEDS_MANUAL_REVIEW"


def compile_c(case_dir: Path, source_file: str, timeout: int) -> tuple[list[dict], bool]:
    compile_commands = [
        ["cc", "-fsanitize=address,undefined", "-g", "-O0", "-Wall", "-Wextra", source_file, "-o", "poc", "-lcurl", "-lpthread"],
        ["cc", "-g", "-O0", "-Wall", "-Wextra", source_file, "-o", "poc", "-lcurl", "-lpthread"],
    ]
    results = []
    for command in compile_commands:
        result = run_command(command, cwd=case_dir, timeout=timeout)
        results.append(result)
        if result["exit_code"] == 0:
            return results, True
    return results, False


def commands_from_harness(harness: dict) -> tuple[list[str], list[str], str | None]:
    if not harness.get("can_generate"):
        return [], [], "llm_harness cannot generate runnable local PoC"
    language = harness.get("language")
    if language == "c":
        filename = harness.get("filename") or "poc.c"
        return harness.get("build_commands") or [], harness.get("run_commands") or [f"./{Path(filename).stem}"], None
    if language in {"python", "shell"}:
        return harness.get("build_commands") or [], harness.get("run_commands") or [], None
    return [], [], f"unsupported harness language: {language}"


def allows_default_curl_cli(scope_gate: dict | None) -> bool:
    return bool(
        scope_gate
        and scope_gate.get("should_run_in_docker")
        and scope_gate.get("scope") in CURL_CLI_SCOPES
    )


def parse_curl_cli_command(command: str) -> list[str] | None:
    """Parse one direct curl command without invoking a shell interpreter."""
    if not isinstance(command, str):
        return None
    stripped = command.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if re.search(r"[\r\n|;&<>`]", stripped) or "$(" in stripped:
        return None
    try:
        argv = shlex.split(stripped, posix=True)
    except ValueError:
        return None
    if not argv or Path(argv[0]).name.lower() not in CURL_CLI_BINARIES:
        return None
    return argv


def parse_curl_cli_commands(commands: list[str], label: str) -> tuple[list[list[str]], str | None]:
    if not isinstance(commands, list) or not commands:
        return [], f"curl CLI harness has no {label} command"
    parsed: list[list[str]] = []
    for command in commands:
        argv = parse_curl_cli_command(command)
        if argv is None:
            return [], f"curl CLI command rejected in {label}: {command!r}"
        parsed.append(argv)
    return parsed, None


def read_curl_cli_script(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def find_llm_harness(case_dir: Path) -> tuple[Path | None, Path]:
    harness_path = case_dir / "llm" / "harness.json"
    if harness_path.exists():
        return harness_path, harness_path.parent
    return None, case_dir


def run_case(case_dir: Path, timeout: int, allow_shell: bool) -> dict:
    case_meta_path = case_dir / "case.json"
    case_meta = load_json(case_meta_path) if case_meta_path.exists() else {}
    report_id = case_meta.get("report_id") or case_dir.name.replace("hackerone_", "")
    result = {
        "report_id": report_id,
        "case_dir": str(case_dir),
        "title": case_meta.get("title"),
        "kind": case_meta.get("kind"),
        "compile": [],
        "run": [],
        "skipped_reason": None,
        "verdict": None,
    }

    environment_path = case_dir / "reproduction_environment.json"
    requested_environment = load_json(environment_path) if environment_path.exists() else {
        "match_status": "VERSION_UNSPECIFIED",
        "allow_execution": True,
    }
    result["reproduction_environment"] = detect_curl_environment(requested_environment)
    if not result["reproduction_environment"].get("allow_execution", False):
        result["skipped_reason"] = (
            "ENVIRONMENT_UNAVAILABLE: "
            + result["reproduction_environment"].get("match_status", "UNKNOWN")
        )
        result["verdict"] = "NEEDS_MANUAL_REVIEW"
        return result

    scope_gate = load_scope_gate(case_dir)
    if scope_gate is not None:
        result["curl_scope"] = scope_gate
        if not scope_gate.get("should_run_in_docker"):
            result["skipped_reason"] = f"curl scope gate rejected: {scope_gate.get('scope')}"
            result["verdict"] = "OUT_OF_SCOPE_REJECT"
            return result

    harness_path, harness_cwd = find_llm_harness(case_dir)
    if harness_path is not None:
        harness = load_json(harness_path)
        result["llm_harness"] = {
            "can_generate": harness.get("can_generate"),
            "language": harness.get("language"),
            "filename": harness.get("filename"),
            "confidence": harness.get("confidence"),
            "expected_observation": harness.get("expected_observation"),
            "assumptions": harness.get("assumptions"),
            "limits": harness.get("limits"),
        }
        build_commands, run_commands, skipped = commands_from_harness(harness)
        if skipped:
            result["skipped_reason"] = skipped
        elif harness.get("language") == "shell" and allows_default_curl_cli(scope_gate):
            direct_build, skipped = parse_curl_cli_commands(build_commands, "build") if build_commands else ([], None)
            direct_run, run_skipped = parse_curl_cli_commands(run_commands, "run")
            skipped = skipped or run_skipped
            if skipped:
                result["skipped_reason"] = skipped
            else:
                for command in direct_build:
                    result["compile"].append(run_command(command, harness_cwd, timeout))
                    if result["compile"][-1]["exit_code"] not in (0, None):
                        break
                if not result["compile"] or result["compile"][-1]["exit_code"] == 0:
                    for command in direct_run:
                        result["run"].append(run_command(command, harness_cwd, timeout))
        elif harness.get("language") == "shell" and not allow_shell:
            result["skipped_reason"] = "non-curl shell harness execution disabled; pass --allow-shell to run"
        else:
            for command in build_commands:
                result["compile"].append(run_command(command, harness_cwd, timeout, shell=True))
                if result["compile"][-1]["exit_code"] not in (0, None):
                    break
            if not result["compile"] or result["compile"][-1]["exit_code"] == 0:
                for command in run_commands:
                    result["run"].append(run_command(command, harness_cwd, timeout, shell=True))
    elif (case_dir / "poc.c").exists():
        compile_results, ok = compile_c(case_dir, "poc.c", timeout)
        result["compile"].extend(compile_results)
        if ok:
            result["run"].append(run_command(["./poc"], case_dir, timeout))
    elif (case_dir / "run.sh").exists():
        if allows_default_curl_cli(scope_gate):
            direct_commands, skipped = parse_curl_cli_commands(read_curl_cli_script(case_dir / "run.sh"), "run.sh")
            if skipped:
                result["skipped_reason"] = skipped
            else:
                result["run"].extend(run_command(command, case_dir, timeout) for command in direct_commands)
        elif allow_shell:
            result["run"].append(run_command(["bash", "run.sh"], case_dir, timeout))
        else:
            result["skipped_reason"] = "non-curl shell case execution disabled; pass --allow-shell to run"
    else:
        result["skipped_reason"] = "no runnable PoC file"

    result["verdict"] = classify(result["run"], result["compile"], result["skipped_reason"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Compile/run local PoC candidates inside a container")
    parser.add_argument("--cases-dir", default="/workspace/cases")
    parser.add_argument("--out", default="/workspace/results/results.jsonl")
    parser.add_argument("--report-id", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--allow-shell", action="store_true")
    args = parser.parse_args()

    cases_dir = Path(args.cases_dir)
    selected = set(args.report_id)
    case_dirs = sorted(path for path in cases_dir.glob("hackerone_*") if path.is_dir())
    if selected:
        case_dirs = [path for path in case_dirs if path.name.replace("hackerone_", "") in selected]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = {}
    with out.open("w", encoding="utf-8") as f:
        for case_dir in case_dirs:
            item = run_case(case_dir, args.timeout, args.allow_shell)
            summary[item["verdict"]] = summary.get(item["verdict"], 0) + 1
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
            print(f"{item['report_id']}: {item['verdict']}", flush=True)

    (out.parent / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
