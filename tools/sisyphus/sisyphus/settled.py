"""Settledness: a property of a run record plus its stop record, shared by ``status`` and archiving."""

from __future__ import annotations

from sisyphus.resume import is_stopped
from sisyphus.runrecord import GATE_FAILED_KEY, STAGE_COMPLETE, RunRecord
from sisyphus.stages import OPEN_PR_STAGE
from sisyphus.stoprecord import StopRecord, is_abandoned

# Pre-#129 stage that no longer exists in STAGE_ORDER; old run records still carry it.
LEGACY_FINISHED_STAGE = "ci-fix"


def is_settled(rec: RunRecord, stop: StopRecord | None) -> bool:
    """Settled: abandoned (no checkpoint), or finished (``open-pr`` or legacy ``ci-fix`` complete) and not stopped."""
    if rec.checkpoint is not None:
        return False
    if rec.retries.get(GATE_FAILED_KEY) and not is_abandoned(stop):
        return False  # a failed gate leaves no checkpoint; keep the item visible
    if is_abandoned(stop):
        return True
    finished_stage = rec.stage in (OPEN_PR_STAGE, LEGACY_FINISHED_STAGE)
    return finished_stage and rec.stage_status == STAGE_COMPLETE and not is_stopped(rec, stop)
