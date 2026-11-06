"""Shared fixtures."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from dotharness.sisyphus import cli
from dotharness.sisyphus.digest import DigestReport, NeedsYou
from dotharness.sisyphus.orchestrator import PullRequest, RunResult
from dotharness.sisyphus.runrecord import RunRecord


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """An installed ``~/.harness/knowledge`` must not shadow the packaged prompts under test."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


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
            return RunResult(item, cp, "why", ("intake",), (), "b", None, None, rec, report)
        pr = PullRequest(7, "http://pr/7")
        return RunResult(item, None, "", ("intake",), (), "b", pr, None, rec, DigestReport())

    monkeypatch.setattr(cli, "run_item", fake)
    return state


FORBIDDEN_IMPORTS = (
    "dotharness.pr_review",
    "dotharness.vibe_heal",
    "dotharness.spec_prism_flow",
    "harness",
    "spec_prism_flow",
)


def forbidden_imports(source: str) -> set[str]:
    """Imported names in ``source`` that equal or sit under a forbidden package."""
    mods: set[str] = set()
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module)
            mods |= {f"{n.module}.{a.name}" for a in n.names}
        elif isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
    return {m for m in mods if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IMPORTS)}
