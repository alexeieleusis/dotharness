from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from spec_prism_flow.build import phase_runner
from spec_prism_flow.build.completion_log import COMPLETION_LOG_JSON_RELPATH, load_all
from spec_prism_flow.build.errors import OrchestrationError
from spec_prism_flow.build.phase_corpus import discover_phase_files, merged_phase_numbers, phase_number_from_path
from spec_prism_flow.build.phase_runner import PhaseRunResult
from spec_prism_flow.phase_file import parse_phase_file

if TYPE_CHECKING:
    from spec_prism_flow.config import SpecPrismFlowConfig


def already_merged_phase_numbers(completion_log_path: Path) -> set[int]:
    """`set()`, not an exception, when the completion log doesn't exist yet -- the
    normal case before any phase has merged."""
    return merged_phase_numbers(load_all(completion_log_path))


def run_track(
    config: SpecPrismFlowConfig,
    clone: Path,
    *,
    start_phase: int | None = None,
    stop_phase: int | None = None,
    dry_run: bool = False,
    resume: bool = False,
    strict: bool = False,
) -> list[PhaseRunResult]:
    """Runs `discover_phase_files(config.plan.phase_dir)` in order against `clone`,
    skipping phases outside the optional `[start_phase, stop_phase]` inclusive bounds
    and phases already merged per `clone`'s completion log, calling Phase 11's
    `run_phase` for the rest. An `OrchestrationError` from `run_phase` is annotated
    with `phase_number`/`phase_name` (via `OrchestrationError.with_context`) before
    it propagates uncaught, halting the run."""
    merged = already_merged_phase_numbers(clone / COMPLETION_LOG_JSON_RELPATH)

    results: list[PhaseRunResult] = []
    for phase_path in discover_phase_files(config.plan.phase_dir):
        number = phase_number_from_path(phase_path)
        if start_phase is not None and number < start_phase:
            continue
        if stop_phase is not None and number > stop_phase:
            continue
        if number in merged:
            continue

        phase = parse_phase_file(phase_path)
        try:
            result = phase_runner.run_phase(clone, config, phase, dry_run=dry_run, resume=resume, strict=strict)
        except OrchestrationError as exc:
            exc.with_context(phase_number=phase.number, phase_name=phase.name)
            raise
        results.append(result)
    return results
