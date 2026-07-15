from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from .curl_runtime import CurlRuntime, prepare_build_context, resolve_curl_runtime
from .extract_candidates import extract_candidates
from .io_utils import load_jsonl, safe_case_id
from .llm_judge import judge_results
from .llm_harness import DEFAULT_MODEL, generate_harnesses, print_selected, select_records
from .models import PipelineResult


PACKAGE_DIR = Path(__file__).resolve().parent
DOCKER_CONTEXT = PACKAGE_DIR / "docker"
TESTCASE_DIR = PACKAGE_DIR / "testcases"
DEFAULT_IMAGE = "bugbounty-poc-repro"


@contextmanager
def pipeline_stage(name: str, callback: Callable | None):
    """Emit best-effort timing events around one pipeline stage."""
    started = time.perf_counter()
    if callback:
        callback("started", name, {"stage": name})
    try:
        yield
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if callback:
            callback("failed", name, {
                "stage": name,
                "duration_ms": duration_ms,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            })
        raise
    else:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if callback:
            callback("completed", name, {"stage": name, "duration_ms": duration_ms})


def display_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return " ".join(subprocess.list2cmdline([part]) for part in command)


def as_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def resolve_testcase(testcase: str | Path) -> Path:
    testcase_path = Path(testcase).expanduser()
    if testcase_path.exists():
        return testcase_path.resolve()

    name = str(testcase)
    candidates = [
        TESTCASE_DIR / f"{name}.jsonl",
        TESTCASE_DIR / name / "parsed_reports.jsonl",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    available = sorted(path.stem for path in TESTCASE_DIR.glob("*.jsonl"))
    raise ValueError(f"Unknown testcase: {testcase}. Available testcases: {available}")


def run_command(command: list[str], cwd: Path, dry_run: bool) -> None:
    print(f"$ {display_command(command)}", flush=True)
    if dry_run:
        return
    try:
        subprocess.run(command, cwd=str(cwd), check=True)
    except FileNotFoundError as exc:
        if command and command[0] == "docker":
            raise RuntimeError("Docker CLI를 찾지 못했습니다. Docker Desktop 설치/실행 상태를 확인하세요.") from exc
        raise
    except subprocess.CalledProcessError as exc:
        if command and command[0] == "docker":
            print(
                "\nDocker 명령이 실패했습니다. Docker Desktop 실행 상태와 docker-users 권한을 확인하세요.",
                file=sys.stderr,
            )
        raise


def docker_image_exists(image: str) -> bool:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", image],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Docker CLI를 찾지 못했습니다. Docker Desktop 설치/실행 상태를 확인하세요.") from exc
    return completed.returncode == 0


def build_docker_image(image: str, no_cache: bool, dry_run: bool) -> bool:
    command = ["docker", "build", "-t", image]
    if no_cache:
        command.append("--no-cache")
    command.append(str(DOCKER_CONTEXT))
    run_command(command, cwd=DOCKER_CONTEXT, dry_run=dry_run)
    return not dry_run


def ensure_docker_image(image: str, no_cache: bool, rebuild: bool, dry_run: bool) -> bool:
    if dry_run:
        print(f"dry_run: would reuse image if present, otherwise build {image}")
        return False
    if not rebuild and not no_cache and docker_image_exists(image):
        print(f"reusing Docker image: {image}", flush=True)
        return False
    return build_docker_image(image, no_cache=no_cache, dry_run=dry_run)


def select_curl_runtime(parsed_path: Path, report_ids: list[str], repo_path: str | Path | None, image_prefix: str) -> tuple[CurlRuntime, list[str]]:
    selected = set(report_ids)
    records = [record for record in load_jsonl(parsed_path) if not selected or record["source_record"]["report_id"] in selected]
    selected_ids = [record["source_record"]["report_id"] for record in records]
    version_values = list(dict.fromkeys(
        str(record.get("parser", {}).get("affected_version") or "").strip()
        for record in records
    ))
    versions = [version for version in version_values if version]
    if len(version_values) > 1:
        return CurlRuntime(
            requested_value=", ".join(versions), requested_curl_version=None,
            requested_libcurl_version=None, resolved_git_tag=None, image=None,
            match_status="MULTIPLE_VERSION_REQUIREMENTS", allow_execution=False,
        ), selected_ids
    return resolve_curl_runtime(versions[0] if versions else None, repo_path, image_prefix), selected_ids


def write_runtime_metadata(cases_dir: Path, report_ids: list[str], runtime: CurlRuntime) -> None:
    payload = runtime.as_dict()
    for report_id in report_ids:
        case_dir = cases_dir / safe_case_id(report_id)
        if case_dir.exists():
            (case_dir / "reproduction_environment.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )


def ensure_versioned_image(runtime: CurlRuntime, repo_path: Path, work_path: Path, no_cache: bool, rebuild: bool, dry_run: bool) -> bool:
    if not runtime.image or not runtime.resolved_git_tag:
        raise ValueError("versioned image requires an exact curl runtime")
    if dry_run:
        print(f"dry_run: would build/reuse {runtime.image} from {runtime.resolved_git_tag}")
        return False
    if not rebuild and not no_cache and docker_image_exists(runtime.image):
        print(f"reusing version-matched Docker image: {runtime.image}", flush=True)
        return False
    context_dir = work_path / "curl-image-context"
    prepare_build_context(repo_path, runtime.resolved_git_tag, context_dir, DOCKER_CONTEXT)
    command = ["docker", "build", "-t", runtime.image]
    if no_cache:
        command.append("--no-cache")
    command.append(str(context_dir))
    run_command(command, cwd=context_dir, dry_run=False)
    return True


def run_docker_cases(
    image: str,
    cases_dir: Path,
    results_dir: Path,
    report_ids: list[str],
    timeout: int,
    allow_shell: bool,
    memory: str,
    cpus: str,
    dry_run: bool,
) -> bool:
    if not dry_run:
        results_dir.mkdir(parents=True, exist_ok=True)

    command = [
        "docker",
        "run",
        "--rm",
        "--pids-limit",
        "128",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--network",
        "none",
        "--memory",
        memory,
        "--cpus",
        cpus,
        "-v",
        f"{cases_dir}:/workspace/cases:rw",
        "-v",
        f"{results_dir}:/workspace/results:rw",
        image,
        "--cases-dir",
        "/workspace/cases",
        "--out",
        "/workspace/results/results.jsonl",
        "--timeout",
        str(timeout),
    ]
    for report_id in report_ids:
        command.extend(["--report-id", report_id])
    if allow_shell:
        command.append("--allow-shell")
    run_command(command, cwd=results_dir.parent, dry_run=dry_run)
    return not dry_run


def run_pipeline(
    parsed_jsonl: str | Path | None = None,
    work_dir: str | Path | None = None,
    *,
    testcase: str | Path | None = None,
    clean: bool = False,
    report_ids: list[str] | None = None,
    with_llm: bool = True,
    llm_limit: int | None = None,
    force_llm: bool = False,
    llm_sleep: float = 0.2,
    judge_with_llm: bool = True,
    judge_limit: int | None = None,
    judge_sleep: float = 0.2,
    upstage_api_key: str | None = None,
    key_file: str | Path | None = None,
    upstage_base_url: str = "https://api.upstage.ai/v1",
    model: str = DEFAULT_MODEL,
    image: str = DEFAULT_IMAGE,
    curl_repo_path: str | Path | None = None,
    build_docker: bool = True,
    rebuild_image: bool = False,
    no_cache: bool = False,
    run_docker: bool = True,
    timeout: int = 20,
    allow_shell: bool = False,
    memory: str = "512m",
    cpus: str = "1",
    dry_run: bool = False,
    event_callback: Callable | None = None,
) -> PipelineResult:
    if testcase is not None and parsed_jsonl is not None:
        raise ValueError("Use either testcase or parsed_jsonl, not both.")
    if testcase is not None:
        parsed_path = resolve_testcase(testcase)
        if work_dir is None:
            work_path = (Path.cwd() / ".poc_repro_runs" / parsed_path.stem).resolve()
        else:
            work_path = as_path(work_dir)
    else:
        if parsed_jsonl is None:
            raise ValueError("parsed_jsonl is required when testcase is not provided.")
        if work_dir is None:
            raise ValueError("work_dir is required when testcase is not provided.")
        parsed_path = as_path(parsed_jsonl)
        work_path = as_path(work_dir)
    cases_dir = work_path / "cases"
    results_dir = work_path / "results"
    report_id_list = report_ids or []
    resolved_repo_path = curl_repo_path or os.getenv("POC_CURL_REPO_PATH")
    curl_runtime, runtime_report_ids = select_curl_runtime(parsed_path, report_id_list, resolved_repo_path, image)
    effective_image = curl_runtime.image or image

    print("[1/5] extract PoC candidates and reporter claims", flush=True)
    manifest = None
    with pipeline_stage("candidate_extraction", event_callback):
        if dry_run:
            print(f"dry_run: would read {parsed_path}")
            print(f"dry_run: would write {cases_dir}")
        else:
            manifest = extract_candidates(parsed_path, cases_dir, clean=clean)
            print(
                f"candidate_count={manifest['candidate_count']} "
                f"runnable_count={manifest['runnable_count']} "
                f"manual_count={manifest['manual_count']}",
                flush=True,
            )
            write_runtime_metadata(cases_dir, runtime_report_ids, curl_runtime)

    print("[2/5] LLM harness generation", flush=True)
    llm_selected_count = 0
    with pipeline_stage("harness_generation", event_callback):
        if with_llm:
            records = select_records(
                parsed_path=parsed_path,
                cases_dir=cases_dir,
                report_ids=set(report_id_list),
                limit=llm_limit,
                generate=True,
                force=force_llm,
            )
            llm_selected_count = len(records)
            print(f"selected_for_llm {llm_selected_count}")
            print_selected(records, cases_dir)
            if dry_run:
                print("dry_run: would call LLM and write llm/harness.json")
            else:
                generate_harnesses(
                    records,
                    cases_dir,
                    api_key=upstage_api_key,
                    key_file=key_file,
                    base_url=upstage_base_url,
                    model=model,
                    sleep_seconds=llm_sleep,
                )
        else:
            print("skipped: with_llm=False")

    print("[3/5] docker build", flush=True)
    docker_built = False
    with pipeline_stage("docker_build", event_callback):
        if build_docker:
            if curl_runtime.allow_execution and curl_runtime.image and curl_runtime.resolved_git_tag:
                docker_built = ensure_versioned_image(
                    curl_runtime, Path(resolved_repo_path), work_path,
                    no_cache=no_cache, rebuild=rebuild_image, dry_run=dry_run,
                )
            else:
                docker_built = ensure_docker_image(image, no_cache=no_cache, rebuild=rebuild_image, dry_run=dry_run)
        else:
            print("skipped: build_docker=False")

    print("[4/5] docker run", flush=True)
    docker_ran = False
    with pipeline_stage("docker_run", event_callback):
        if run_docker:
            docker_ran = run_docker_cases(
                image=effective_image,
                cases_dir=cases_dir,
                results_dir=results_dir,
                report_ids=report_id_list,
                timeout=timeout,
                allow_shell=allow_shell,
                memory=memory,
                cpus=cpus,
                dry_run=dry_run,
            )
        else:
            print("skipped: run_docker=False")

    print("[5/5] LLM claim/result judgement", flush=True)
    results_path = results_dir / "results.jsonl"
    judgement_path = results_dir / "judgements.jsonl"
    llm_judgement_count = 0
    with pipeline_stage("result_judgement", event_callback):
        if judge_with_llm:
            if dry_run:
                print(f"dry_run: would compare {results_path} with cases/*/claim.json")
                print(f"dry_run: would write {judgement_path}")
            elif not results_path.exists():
                print(f"skipped: judge_with_llm=True but results file is missing: {results_path}")
            else:
                llm_judgement_count = judge_results(
                    results_path=results_path,
                    cases_dir=cases_dir,
                    out_path=judgement_path,
                    api_key=upstage_api_key,
                    key_file=key_file,
                    base_url=upstage_base_url,
                    model=model,
                    report_ids=report_id_list,
                    limit=judge_limit,
                    sleep_seconds=judge_sleep,
                )
                print(f"judgement_count={llm_judgement_count}", flush=True)
        else:
            print("skipped: judge_with_llm=False")

    print("done", flush=True)
    return PipelineResult(
        manifest=manifest,
        work_dir=work_path,
        cases_dir=cases_dir,
        results_dir=results_dir,
        results_path=results_path,
        summary_path=results_dir / "summary.json",
        judgement_path=judgement_path,
        image=effective_image,
        llm_selected_count=llm_selected_count,
        llm_judgement_count=llm_judgement_count,
        docker_built=docker_built,
        docker_ran=docker_ran,
        dry_run=dry_run,
    )
