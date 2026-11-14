"""``_Run.build_gate_plan``: the gate plan (or the reason there is none) without a PR."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotharness.sisyphus import orchestrator
from dotharness.sisyphus.orchestrator import GATE_FAILED_KEY, OrchestratorError, PullRequest
from tests.sisyphus.test_orchestrator import ITEM, World


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = World(tmp_path)
    (w.repo / "other").mkdir()
    (w.repo / "other" / "Makefile").write_text("x:\n")
    w.changed = "tools/pkg/a.py\nother/b.py\n"
    return w


def _run(world: World) -> orchestrator._Run:
    return orchestrator._Run(
        ITEM,
        world.config(),
        world.options(),
        world.agent,
        world.shell,
        world,
        world.clock,
        world,
    )


def _spf(world: World, text: str | None) -> None:
    path = world.repo / ".spec-prism-flow.toml"
    if text is None:
        path.unlink()
    else:
        path.write_text(text)


def _diffs(world: World) -> list[tuple[str, ...]]:
    return [c[0] for c in world.shell_calls if c[0][:2] == ("git", "diff")]


def _assert_no_side_effects(run: orchestrator._Run) -> None:
    assert run.notes == []
    assert run.gated is False
    assert run.gate_failure is None
    assert GATE_FAILED_KEY not in run.rec.retries


async def test_no_pr_needed(world: World) -> None:
    run = _run(world)
    assert run.pr is None
    plan, reason = await run.build_gate_plan()
    assert reason is None
    assert plan
    assert world.find_calls == 0
    assert run.pr is None


async def test_no_config(world: World) -> None:
    _spf(world, None)
    run = _run(world)
    assert await run.build_gate_plan() == ([], f"no build.commands in {run.spf}")
    assert _diffs(world) == []
    _assert_no_side_effects(run)


async def test_no_changed_files(world: World) -> None:
    world.changed = ""
    run = _run(world)
    assert await run.build_gate_plan() == ([], "no changed files to gate")
    _assert_no_side_effects(run)


async def test_only_entries_none_applies(world: World) -> None:
    _spf(
        world,
        '[[build.gate]]\nrun = "make a"\npackages = ["elsewhere"]\n\n[[build.gate]]\nrun = "make b"\npackages = ["nope"]\n',
    )
    run = _run(world)
    assert await run.build_gate_plan() == (
        [],
        "no [[build.gate]] entry applies to the touched package directories",
    )
    _assert_no_side_effects(run)


async def test_mixed_config(world: World) -> None:
    _spf(
        world,
        '[build]\ncommands = ["make check", "make test"]\n\n'
        '[[build.gate]]\nrun = "make scoped"\npackages = ["tools/pkg"]\n\n'
        '[[build.gate]]\nrun = "make all"\n',
    )
    run = _run(world)
    plan, reason = await run.build_gate_plan()
    assert reason is None
    other, pkg = world.repo / "other", world.repo / "tools" / "pkg"
    assert plan == [
        (other, "make check"),
        (other, "make test"),
        (other, "make all"),
        (pkg, "make check"),
        (pkg, "make test"),
        (pkg, "make scoped"),
        (pkg, "make all"),
    ]
    _assert_no_side_effects(run)


async def test_diff_base(world: World) -> None:
    run = _run(world)
    await run.build_gate_plan()
    assert _diffs(world) == [("git", "diff", "--name-only", f"origin/{run.opt.base_branch}...HEAD")]
    assert run.opt.base_branch == "base-branch"


# ------------------------------------------------------------------ gate() delegates to build_gate_plan


def _gate_run(world: World) -> orchestrator._Run:
    world.existing_pr = PullRequest(42, "https://example.test/pr/42")
    return _run(world)


async def test_gate_no_config_note(world: World) -> None:
    _spf(world, None)
    run = _gate_run(world)
    await run.gate()
    assert run.notes == [f"gate not run for PR #42: no build.commands in {run.spf}"]
    assert run.gated is False
    assert _diffs(world) == []


async def test_gate_no_changed_files_note(world: World) -> None:
    world.changed = ""
    run = _gate_run(world)
    await run.gate()
    assert run.notes == ["gate not run for PR #42: no changed files to gate"]
    assert run.gated is False


async def test_gate_no_entry_applies_note(world: World) -> None:
    _spf(world, '[[build.gate]]\nrun = "make a"\npackages = ["elsewhere"]\n')
    run = _gate_run(world)
    await run.gate()
    assert run.notes == ["gate not run for PR #42: no [[build.gate]] entry applies to the touched package directories"]
    assert run.gated is False


async def test_gate_passing_clears_failure_flag(world: World) -> None:
    run = _gate_run(world)
    run.rec.retries[GATE_FAILED_KEY] = 1
    await run.gate()
    assert run.gated is True
    assert run.rec.retries[GATE_FAILED_KEY] == 0
    assert run.notes == []


async def test_gate_failing_records_failure(world: World) -> None:
    world.gate_rc = 2
    run = _gate_run(world)
    await run.gate()
    assert run.gated is False
    assert run.rec.retries[GATE_FAILED_KEY] == 1
    assert len(run.notes) == 1
    assert run.notes[0].startswith("gate command 'make check' failed in ")
    assert run.notes[0].endswith("(exit 2): gate failed")


async def test_gate_without_pr_raises_before_any_config_or_git(world: World) -> None:
    run = _run(world)
    with pytest.raises(OrchestratorError, match="no open PR found"):
        await run.gate()
    assert _diffs(world) == []
    assert not run.spf.exists()
    assert world.find_calls == 1
