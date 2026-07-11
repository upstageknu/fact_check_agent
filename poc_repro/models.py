from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class PipelineResult:
    manifest: dict[str, Any] | None
    work_dir: Path
    cases_dir: Path
    results_dir: Path
    results_path: Path
    summary_path: Path
    judgement_path: Path
    image: str
    llm_selected_count: int
    llm_judgement_count: int
    docker_built: bool
    docker_ran: bool
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, Path):
                data[key] = str(value)
        return data
