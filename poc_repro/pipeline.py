from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .extract_candidates import extract_candidates
from .llm_judge import judge_results
from .llm_harness import DEFAULT_MODEL, generate_harnesses, print_selected, select_records
from .models import PipelineResult


PACKAGE_DIR = Path(__file__).resolve().parent
DOCKER_CONTEXT = PACKAGE_DIR / "docker"
TESTCASE_DIR = PACKAGE_DIR / "testcases"
DEFAULT_IMAGE = "bugbounty-poc-repro"


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
    build_docker: bool = True,
    rebuild_image: bool = False,
    no_cache: bool = False,
    run_docker: bool = True,
    timeout: int = 20,
    allow_shell: bool = False,
    memory: str = "512m",
    cpus: str = "1",
    dry_run: bool = False,
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

    print("[1/5] extract PoC candidates and reporter claims", flush=True)
    manifest = None
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

    print("[2/5] LLM harness generation", flush=True)
    llm_selected_count = 0
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
    if build_docker:
        docker_built = ensure_docker_image(image, no_cache=no_cache, rebuild=rebuild_image, dry_run=dry_run)
    else:
        print("skipped: build_docker=False")

    print("[4/5] docker run", flush=True)
    docker_ran = False
    if run_docker:
        docker_ran = run_docker_cases(
            image=image,
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
        image=image,
        llm_selected_count=llm_selected_count,
        llm_judgement_count=llm_judgement_count,
        docker_built=docker_built,
        docker_ran=docker_ran,
        dry_run=dry_run,
    )
