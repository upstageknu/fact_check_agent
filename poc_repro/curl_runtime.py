from __future__ import annotations

import re
import shutil
import subprocess
import tarfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


VERSION_PATTERN = re.compile(r"(?<![0-9.])(\d+\.\d+\.\d+)(?![0-9.])")
RANGE_MARKERS = ("before", "after", "prior", "through", "below", "above", "<", ">", "~")


@dataclass
class CurlRuntime:
    requested_value: str | None
    requested_curl_version: str | None
    requested_libcurl_version: str | None
    resolved_git_tag: str | None
    image: str | None
    match_status: str
    allow_execution: bool
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def resolve_requested_version(value: Any) -> tuple[str | None, str]:
    if value is None:
        return None, "VERSION_UNSPECIFIED"
    if isinstance(value, (list, tuple, set)):
        raw = " ".join(str(item) for item in value if item is not None).strip()
    else:
        raw = str(value).strip()
    if not raw:
        return None, "VERSION_UNSPECIFIED"

    versions = list(dict.fromkeys(VERSION_PATTERN.findall(raw)))
    lowered = raw.casefold()
    if len(versions) != 1:
        return None, "VERSION_AMBIGUOUS" if versions else "VERSION_UNRESOLVED"
    if any(marker in lowered for marker in RANGE_MARKERS):
        return None, "VERSION_RANGE_UNRESOLVED"
    return versions[0], "VERSION_RESOLVED"


def curl_tag(version: str) -> str:
    return "curl-" + version.replace(".", "_")


def versioned_image_name(image_prefix: str, version: str) -> str:
    last_slash = image_prefix.rfind("/")
    last_colon = image_prefix.rfind(":")
    repository = image_prefix[:last_colon] if last_colon > last_slash else image_prefix
    return f"{repository}:curl-{version}"


def git_tag_exists(repo_path: Path, tag: str) -> bool:
    completed = subprocess.run(
        ["git", "-c", f"safe.directory={repo_path}", "-C", str(repo_path), "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        text=True,
        capture_output=True,
        check=False,
    )
    return completed.returncode == 0


def resolve_curl_runtime(affected_version: Any, repo_path: str | Path | None, image_prefix: str) -> CurlRuntime:
    raw = None if affected_version is None else str(affected_version)
    version, status = resolve_requested_version(affected_version)
    if version is None:
        return CurlRuntime(raw, None, None, None, None, status, status == "VERSION_UNSPECIFIED")

    tag = curl_tag(version)
    if repo_path is None:
        return CurlRuntime(raw, version, version, tag, None, "REPOSITORY_UNAVAILABLE", False)
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return CurlRuntime(raw, version, version, tag, None, "REPOSITORY_UNAVAILABLE", False)
    if not git_tag_exists(repo, tag):
        return CurlRuntime(raw, version, version, tag, None, "VERSION_NOT_FOUND", False)
    return CurlRuntime(raw, version, version, tag, versioned_image_name(image_prefix, version), "EXACT", True)


def prepare_build_context(repo_path: Path, tag: str, context_dir: Path, docker_dir: Path) -> None:
    if context_dir.exists():
        shutil.rmtree(context_dir)
    source_dir = context_dir / "curl-src"
    source_dir.mkdir(parents=True)
    archive_path = context_dir / "curl-source.tar"
    subprocess.run(
        ["git", "-c", f"safe.directory={repo_path}", "-C", str(repo_path), "archive", "--format=tar", f"--output={archive_path}", tag],
        check=True,
    )
    with tarfile.open(archive_path) as archive:
        archive.extractall(source_dir, filter="data")
    archive_path.unlink()
    shutil.copy2(docker_dir / "runner.py", context_dir / "runner.py")
    shutil.copy2(docker_dir / "Dockerfile.versioned", context_dir / "Dockerfile")
