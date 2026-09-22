from __future__ import annotations

from pathlib import Path

from spec_prism_flow.build.completion_log import CompletionRecord
from spec_prism_flow.phase_file import PHASE_FILE_NAME_PATTERN


def phase_number_from_path(path: Path) -> int:
    match = PHASE_FILE_NAME_PATTERN.match(path.name)
    if match is None:
        raise ValueError(f"'{path.name}' does not match the '<NN>-<slug>-leaf.md' pattern")  # noqa: TRY003
    return int(match.group(1))


def discover_phase_files(phases_dir: Path) -> list[Path]:
    """Every `*-leaf.md` file directly under `phases_dir` matching the `<NN>-<slug>-
    leaf.md` pattern, sorted by its numeric prefix (not lexicographically, so
    `10-...` sorts after `09-...`)."""
    matches = [p for p in phases_dir.glob("*-leaf.md") if PHASE_FILE_NAME_PATTERN.match(p.name)]
    return sorted(matches, key=phase_number_from_path)


def merged_phase_numbers(records: list[CompletionRecord]) -> set[int]:
    return {record.phase_number for record in records if record.pr_merged_at is not None}
