"""``sisyphus resume --reset-retries`` (follow-up 20). All external effects are fakes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sisyphus import orchestrator
from sisyphus.digest import render_digest
from sisyphus.lock import clone_lock
from sisyphus.rails import Checkpoint
from sisyphus.resume import (
    AUTO_RETRY_KEY,
    RESET_KEYS,
    reset_keys_for,
    reset_stage_retries,
)
from sisyphus.runrecord import RunRecord, RunRecordError
from sisyphus.stoprecord import action_for, is_abandoned
from tests.test_cli import items, make_env
from tests.test_orchestrator import ITEM, World, world  # noqa: F401  (fixture)
from tests.test_resume import run, stopped

AT = datetime(2026, 3, 4, 5, 6, tzinfo=UTC)


def build_stopped(env: Any, **extra: int) -> RunRecord:
    rec = stopped(env, "OLD-1", "C5", brief=None, stage="build")
    rec.retries.update({"build:retry-cycles": 3, "branch-created": 1, "plan:leaf-limit": 1, **extra})
    rec.save()
    return rec


def resume(env: Any, *extra: str, ref: str = "OLD-1"):
    return env.invoke("resume", ref, "--config", str(env.config), *extra)


# ------------------------------------------------------------- the mapping


def test_mapping_matches_orchestrator_constants_and_never_lists_branch_created() -> None:
    assert RESET_KEYS["build"][0] == orchestrator.BUILD_RETRY_KEY
    assert set(RESET_KEYS) == set(orchestrator.STAGE_ORDER)
    everything = {k for keys in RESET_KEYS.values() for k in keys}
    assert orchestrator.BRANCH_CREATED_KEY not in everything
    assert orchestrator.LEAF_LIMIT_KEY not in everything
    assert reset_keys_for(None) == () and reset_keys_for("nonsense") == ()


# ---------------------------------------------------- RunRecord.reset_retries


def test_reset_retries_touches_only_named_keys_and_records_the_event(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    rec.mark_stage("build")
    rec.set_checkpoint("C5")
    rec.retries.update({"a": 3, "b": 2, "z": 0})
    rec.save()
    changed = rec.reset_retries(["a", "z", "missing"], stage="build", now=AT)
    assert changed == {"a": 3}
    again = RunRecord.open(tmp_path, "X-1")
    assert again.retries == {"a": 0, "b": 2, "z": 0}
    assert (again.stage, again.stage_status, again.checkpoint) == ("build", "in_progress", "C5")
    assert again.resets == [{"at": AT.isoformat(), "stage": "build", "keys": {"a": {"from": 3, "to": 0}}}]


def test_record_without_resets_keeps_its_old_shape_and_loading_never_resets(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    rec.increment_retry("build:retry-cycles")
    assert "resets" not in json.loads(rec.path.read_text())
    assert RunRecord.open(tmp_path, "X-1").retries == {"build:retry-cycles": 1}
    assert RunRecord.open(tmp_path, "X-1").resets == []


def test_bad_resets_field_is_rejected(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    data = json.loads(rec.path.read_text())
    data["resets"] = "nope"
    rec.path.write_text(json.dumps(data))
    with pytest.raises(RunRecordError, match="resets"):
        RunRecord.open(tmp_path, "X-1")


def test_noop_reset_is_recorded_with_no_keys(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    rec.mark_stage("intake")
    assert reset_stage_retries(rec, AT) == {}
    assert RunRecord.open(tmp_path, "X-1").resets == [{"at": AT.isoformat(), "stage": "intake", "keys": {}}]


# -------------------------------------------------------- per stage keys


def test_build_stage_resets_build_and_auto_retry_only(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    rec.mark_stage("build")
    rec.retries.update({
        "build:retry-cycles": 3,
        AUTO_RETRY_KEY: 2,
        "branch-created": 1,
        "plan:leaf-limit": 1,
    })
    rec.save()
    assert reset_stage_retries(rec, AT) == {"build:retry-cycles": 3, AUTO_RETRY_KEY: 2}
    assert RunRecord.open(tmp_path, "X-1").retries == {
        "build:retry-cycles": 0,
        AUTO_RETRY_KEY: 0,
        "branch-created": 1,
        "plan:leaf-limit": 1,
    }


@pytest.mark.parametrize("stage", ["intake", "plan", "push", "open-pr"])
def test_stages_without_a_counter_reset_only_auto_retry(tmp_path: Path, stage: str) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    rec.mark_stage(stage)
    rec.retries.update({"build:retry-cycles": 3, "branch-created": 1, AUTO_RETRY_KEY: 1})
    rec.save()
    assert reset_stage_retries(rec, AT) == {AUTO_RETRY_KEY: 1}
    assert RunRecord.open(tmp_path, "X-1").retries["build:retry-cycles"] == 3


# ----------------------------------------------------------------- the CLI


def test_resume_reset_retries_resets_records_and_continues(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    env.services.now = lambda: AT
    build_stopped(env)
    result = resume(env, "--reset-retries")
    assert result.exit_code == 0, result.output
    assert "reset retries at stage build: build:retry-cycles 3->0" in result.output
    assert items(fake_run_item) == ["OLD-1"]
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    assert rec.retries == {"build:retry-cycles": 0, "branch-created": 1, "plan:leaf-limit": 1}
    assert rec.resets[0]["at"] == AT.isoformat() and rec.resets[0]["stage"] == "build"
    assert "Retry reset: OLD-1 at stage build on 2026-03-04T05:06:00+00:00: build:retry-cycles 3->0" in env.digest
    assert "## Deviations\n\n- Retry reset" in env.digest


def test_status_shows_the_reset(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    env.services.now = lambda: AT
    build_stopped(env)
    fake_run_item["checkpoint"]["OLD-1"] = Checkpoint.C5
    assert resume(env, "--reset-retries").exit_code == 0
    out = env.invoke("status", "OLD-1", "--config", str(env.config)).output
    assert "Retry reset: OLD-1 at stage build on 2026-03-04T05:06:00+00:00: build:retry-cycles 3->0" in out


def test_plain_resume_never_resets(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    build_stopped(env)
    assert resume(env).exit_code == 0
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    assert rec.retries["build:retry-cycles"] == 3 and rec.resets == []
    assert "Retry reset" not in env.digest


def test_auto_resume_in_run_never_resets(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    from datetime import timedelta

    env = make_env(tmp_path)
    env.services.now = lambda: AT + timedelta(hours=1)
    rec = stopped(env, "N-1", "C8", brief=None, stage="build", at=AT)
    rec.retries.update({"build:retry-cycles": 3, AUTO_RETRY_KEY: 1})
    rec.save()
    run(env)
    assert items(fake_run_item) == ["N-1"]
    after = RunRecord.open(tmp_path / "ws", "N-1")
    assert after.retries == {"build:retry-cycles": 3, AUTO_RETRY_KEY: 2}  # only the auto-retry count grew
    assert after.resets == []


def test_reset_with_nothing_to_reset_is_allowed_and_recorded(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    env.services.now = lambda: AT
    stopped(env, "OLD-1", "C1", brief="# B\n\n## Open decisions log\n\n1. Q?\n")
    result = resume(env, "--reset-retries")
    assert result.exit_code == 0, result.output
    assert "nothing to reset" in result.output
    assert RunRecord.open(tmp_path / "ws", "OLD-1").resets[0]["keys"] == {}
    assert "no counters to reset" in env.digest


def test_reset_on_a_not_stopped_item_errors_and_changes_nothing(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    rec = RunRecord.open(tmp_path / "ws", "DONE-1")
    rec.mark_stage("build", "complete")
    rec.retries["build:retry-cycles"] = 2
    rec.save()
    result = resume(env, "--reset-retries", ref="DONE-1")
    assert result.exit_code == 1 and "not stopped" in result.output
    after = RunRecord.open(tmp_path / "ws", "DONE-1")
    assert after.retries["build:retry-cycles"] == 2 and after.resets == []
    assert fake_run_item["calls"] == []


def test_reset_without_record_errors(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    result = resume(env, "--reset-retries", ref="NOPE-1")
    assert result.exit_code == 1 and "no run record" in result.output


def test_reset_combines_with_again_on_an_abandoned_item(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    env.services.now = lambda: AT
    build_stopped(env)
    assert env.invoke("abandon", "OLD-1", "--config", str(env.config)).exit_code == 0
    refused = resume(env, "--reset-retries")
    assert refused.exit_code == 1 and "abandoned" in refused.output
    assert RunRecord.open(tmp_path / "ws", "OLD-1").retries["build:retry-cycles"] == 3  # refusal changed nothing
    ok = resume(env, "--again", "--reset-retries")
    assert ok.exit_code == 0, ok.output
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    assert rec.retries["build:retry-cycles"] == 0 and rec.stage == "build"
    assert not is_abandoned(rec.path)


def test_reset_under_a_held_lock_exits_quietly_without_resetting(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    build_stopped(env)
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        result = resume(env, "--reset-retries")
    assert (result.exit_code, result.output) == (0, "")
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    assert rec.retries["build:retry-cycles"] == 3 and rec.resets == []
    assert fake_run_item["calls"] == []


def test_reset_persists_even_when_the_run_then_fails(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    build_stopped(env)
    fake_run_item["raise"]["OLD-1"] = RuntimeError("boom")
    assert resume(env, "--reset-retries").exit_code == 1
    assert RunRecord.open(tmp_path / "ws", "OLD-1").retries["build:retry-cycles"] == 0


# ---------------------------------------------------- C5 text and digest


def test_c5_action_names_the_exact_reset_command() -> None:
    cmd = "sisyphus resume 'A 1' --config /x/.sisyphus.toml --base main"
    text = action_for("C5", resume_command=cmd)
    assert f"`{cmd} --reset-retries`" in text
    assert "does not skip the build" in text and "fresh budget of max_retry_cycles" in text
    assert "{resume}" not in action_for("C5")


def test_digest_lists_every_reset(tmp_path: Path) -> None:
    rec = RunRecord.open(tmp_path, "X-1")
    rec.mark_stage("build")
    rec.retries["build:retry-cycles"] = 3
    rec.reset_retries(["build:retry-cycles"], stage="build", now=AT)
    rec.reset_retries(["build:retry-cycles"], stage="build", now=AT)
    text = render_digest([rec])
    assert text.count("Retry reset: X-1 at stage build") == 2
    assert "build:retry-cycles 3->0" in text and "no counters to reset" in text


# ----------------------------------------------- full flow with fake agent


def test_c5_exhausted_item_gets_a_fresh_budget_after_reset(world: World) -> None:  # noqa: F811
    world.build_stops = [{"checkpoint": "C5", "detail": "again"}] * 3
    for _ in range(3):
        assert world.run().checkpoint is Checkpoint.C5
        world.branch_exists = True
    builds = world.events.count("agent:build")
    # without a reset: stops at C5 again, no agent launched
    assert world.run().checkpoint is Checkpoint.C5
    assert world.events.count("agent:build") == builds
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    assert rec.stage == "build"
    reset_stage_retries(rec, AT)
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    assert rec.retries["build:retry-cycles"] == 0 and rec.retries["branch-created"] == 1
    world.build_stops = [{"checkpoint": "C5", "detail": "one more"}]
    assert world.run().checkpoint is Checkpoint.C5
    assert world.events.count("agent:build") == builds + 1  # the agent ran again: fresh budget
    assert RunRecord.open(world.tmp / "ws", ITEM).retries["build:retry-cycles"] == 1
    assert "Retry reset: " in world.digest_text()
