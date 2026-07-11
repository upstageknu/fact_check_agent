from __future__ import annotations

import argparse

from .llm_harness import DEFAULT_MODEL
from .pipeline import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-shot parsed JSON -> LLM harness -> Docker compile/run -> LLM judgement pipeline")
    parser.add_argument("--parsed-jsonl", default=None, help="Input parsed reports JSONL")
    parser.add_argument("--testcase", default=None, help="Bundled testcase name, e.g. sample")
    parser.add_argument("--work-dir", default=None, help="Working directory for cases and results")
    parser.add_argument("--clean", action="store_true", help="Recreate work-dir/cases before extracting candidates")
    parser.add_argument("--report-id", action="append", default=[], help="Run/generate only selected report id. Repeatable")
    parser.add_argument("--with-llm", action="store_true", help="Kept for compatibility; LLM harness generation is enabled by default")
    parser.add_argument("--no-llm", action="store_true", help="Debug only: skip LLM harness generation")
    parser.add_argument("--llm-limit", "--limit", dest="llm_limit", type=int, default=None, help="Limit LLM harness generation count")
    parser.add_argument("--force-llm", action="store_true", help="Regenerate existing llm/harness.json files")
    parser.add_argument("--judge-with-llm", action="store_true", help="Kept for compatibility; LLM judgement is enabled by default")
    parser.add_argument("--no-judge", action="store_true", help="Debug only: skip LLM claim/result judgement")
    parser.add_argument("--judge-limit", type=int, default=None, help="Limit LLM judgement count")
    parser.add_argument("--key-file", default=None, help="Path to a file containing UPSTAGE_API_KEY")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"LLM model name. Default: {DEFAULT_MODEL}")
    parser.add_argument("--image", default="bugbounty-poc-repro", help="Docker image name")
    parser.add_argument("--skip-build", action="store_true", help="Skip docker image check/build")
    parser.add_argument("--rebuild", action="store_true", help="Force docker image rebuild even when the image already exists")
    parser.add_argument("--no-cache", action="store_true", help="Pass --no-cache to docker build; also forces rebuild")
    parser.add_argument("--skip-run", action="store_true", help="Skip docker run")
    parser.add_argument("--timeout", type=int, default=20, help="Per-case command timeout in seconds inside the container")
    parser.add_argument(
        "--allow-shell",
        action="store_true",
        help="Allow non-curl shell harness execution; curl CLI is enabled by default",
    )
    parser.add_argument("--memory", default="512m", help="Docker memory limit")
    parser.add_argument("--cpus", default="1", help="Docker CPU limit")
    parser.add_argument("--dry-run", action="store_true", help="Print planned steps without changing files or running Docker")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_pipeline(
        parsed_jsonl=args.parsed_jsonl,
        work_dir=args.work_dir,
        testcase=args.testcase,
        clean=args.clean,
        report_ids=args.report_id,
        with_llm=not args.no_llm,
        llm_limit=args.llm_limit,
        force_llm=args.force_llm,
        judge_with_llm=not args.no_judge,
        judge_limit=args.judge_limit,
        key_file=args.key_file,
        model=args.model,
        image=args.image,
        build_docker=not args.skip_build,
        rebuild_image=args.rebuild,
        no_cache=args.no_cache,
        run_docker=not args.skip_run,
        timeout=args.timeout,
        allow_shell=args.allow_shell,
        memory=args.memory,
        cpus=args.cpus,
        dry_run=args.dry_run,
    )
    print(f"results: {result.results_path}")


if __name__ == "__main__":
    main()
