from __future__ import annotations

from dataclasses import dataclass

import click

from spec_prism_flow.build.errors import ManualTestFailed

_PASSED_PROMPT = "Did every item pass?"
_RETRY_PROMPT = "Spend one more review cycle and retry, instead of escalating now?"


@dataclass(frozen=True)
class ManualTestOutcome:
    passed: bool
    retry: bool
    notes: str | None


def prompt(phase_number: int, phase_name: str, checklist: list[str], *, strict: bool = False) -> ManualTestOutcome:
    """Interactive manual-test gate for a finished phase. `strict` controls only the
    fail-then-decline branch: `strict=True` raises `ManualTestFailed` there (for a
    caller that must block on a human verdict), while the `strict=False` default
    returns a failed, non-retrying, non-raising outcome instead, leaving it to the
    caller to decide whether to proceed toward merge anyway."""
    if not checklist:
        click.echo(f"Phase {phase_number} ({phase_name}): no manual test checklist -- skipping.")
        return ManualTestOutcome(passed=True, retry=False, notes=None)

    click.echo(f"Phase {phase_number} ({phase_name}): manual test checklist")
    for item in checklist:
        click.echo(f"  - {item}")

    if click.confirm(_PASSED_PROMPT, default=False):
        return ManualTestOutcome(passed=True, retry=False, notes=None)

    notes = click.prompt("What failed?", default="") or None

    if click.confirm(_RETRY_PROMPT, default=False):
        return ManualTestOutcome(passed=False, retry=True, notes=notes)

    if strict:
        raise ManualTestFailed(notes)
    return ManualTestOutcome(passed=False, retry=False, notes=notes)
