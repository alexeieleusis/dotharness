"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sisyphus import cli
from sisyphus.digest import DigestReport, NeedsYou
from sisyphus.orchestrator import PullRequest, RunResult
from sisyphus.runrecord import RunRecord


@pytest.fixture
def fake_run_item(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"calls": [], "checkpoint": {}, "raise": {}, "hook": None}

    def fake(item, config, options, **kwargs):
        state["calls"].append((item, options, kwargs))
        if state["hook"]:
            state["hook"](config, options)
        if item in state["raise"]:
            raise state["raise"][item]
        rec = RunRecord.open(Path(config.project.workspace_dir), item)
        cp = state["checkpoint"].get(item)
        if cp:
            rec.set_checkpoint(cp.value)
            report = DigestReport(needs_you=(NeedsYou(item, cp.value, f"stopped {cp.value}"),))
            return RunResult(item, cp, "why", ("intake",), (), "b", None, None, False, None, rec, report)
        pr = PullRequest(7, "http://pr/7")
        return RunResult(item, None, "", ("intake",), (), "b", pr, "s", True, None, rec, DigestReport())

    monkeypatch.setattr(cli, "run_item", fake)
    return state
