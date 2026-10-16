"""``sisyphus abandon`` and the ``resume --again`` re-enable path (follow-up 19). All external effects are fakes."""

# ruff: noqa: TRY003
from __future__ import annotations

from pathlib import Path
from typing import Any

from sisyphus.digest import render_digest
from sisyphus.lock import clone_lock
from sisyphus.orchestrator import BRANCH_CREATED_KEY, OPEN_PR_STAGE, PullRequest, branch_name
from sisyphus.pick import find_awaiting_review, is_started
from sisyphus.resume import is_stopped, list_waiting
from sisyphus.runrecord import RunRecord
from sisyphus.stoprecord import (
    ACTIONS,
    KIND_ABANDONED,
    build_stop_record,
    is_abandoned,
    read_stop_record,
    write_stop_record,
)
from tests.test_cli import FakePrs, items, make_env
from tests.test_resume import run, stopped


class Recorder:
    """Fake subprocess runner that records every command; abandon must not issue any."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(args)
        raise AssertionError("abandon must not run subprocesses")


class SpyPrs(FakePrs):
    def __init__(self, open_branches: dict[str, PullRequest]) -> None:
        super().__init__(open_branches)
        self.calls: list[str] = []

    def find_pr(self, branch: str) -> PullRequest | None:
        self.calls.append(branch)
        return super().find_pr(branch)


def abandon_env(tmp_path: Path, refs: tuple[str, ...] = (), prs: FakePrs | None = None) -> tuple[Any, Recorder]:
    env = make_env(tmp_path, list(refs), prs=prs)
    rec = Recorder()
    env.services.subprocess_runner = rec
    return env, rec


def abandon(env: Any, ref: str = "OLD-1"):
    return env.invoke("abandon", ref, "--config", str(env.config))


def test_abandon_writes_mark_and_clears_waiting(tmp_path: Path) -> None:
    env, sh = abandon_env(tmp_path)
    rec = stopped(env, "OLD-1", "C7", brief=None, stage="push")
    result = abandon(env)
    assert result.exit_code == 0, result.output
    stop = read_stop_record(rec.path)
    assert stop is not None and stop.kind == KIND_ABANDONED and stop.stopped_at
    assert stop.label == "abandoned"
    assert "--again" in stop.resume_command and "OLD-1" in stop.resume_command
    reopened = RunRecord.open(tmp_path / "ws", "OLD-1")
    assert reopened.checkpoint is None and reopened.stage == "push"  # record kept, only the wait removed
    assert list_waiting(tmp_path / "ws") == []
    assert not is_stopped(reopened, stop)
    assert is_abandoned(rec.path)
    assert sh.calls == []


def test_abandon_of_error_stopped_item(tmp_path: Path) -> None:
    env, _ = abandon_env(tmp_path)
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    write_stop_record(
        rec.path,
        build_stop_record(
            "OLD-1", checkpoint=None, stage="build", stage_status="in_progress", detail="x", resume_command="r"
        ),
    )
    assert abandon(env).exit_code == 0
    assert is_abandoned(rec.path)


def test_abandon_issues_no_remote_branch_or_pr_commands(tmp_path: Path) -> None:
    prs = SpyPrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env, sh = abandon_env(tmp_path, prs=prs)
    calls = prs.calls
    rec = stopped(env, "OLD-1", "C7", brief=None, stage=OPEN_PR_STAGE)
    rec.increment_retry(BRANCH_CREATED_KEY)
    brief_dir = tmp_path / "ws"
    before = {p.name for p in brief_dir.iterdir() if not p.name.endswith(".stopped.json")}
    assert abandon(env).exit_code == 0
    assert sh.calls == [] and calls == []
    after = {p.name for p in brief_dir.iterdir()}
    assert before <= after  # run record (and everything else) kept


def test_abandon_twice_is_a_noop(tmp_path: Path) -> None:
    env, _ = abandon_env(tmp_path)
    rec = stopped(env, "OLD-1", "C7", brief=None)
    abandon(env)
    first = read_stop_record(rec.path)
    result = abandon(env)
    assert result.exit_code == 0 and "already abandoned" in result.output
    assert read_stop_record(rec.path) == first


def test_abandon_without_record_errors(tmp_path: Path) -> None:
    env, _ = abandon_env(tmp_path)
    result = abandon(env, "NOPE-1")
    assert result.exit_code == 1 and "no run record for NOPE-1" in result.output


def test_abandon_under_held_lock_exits_zero_quietly(tmp_path: Path) -> None:
    env, _ = abandon_env(tmp_path)
    rec = stopped(env, "OLD-1", "C7", brief=None)
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        result = abandon(env)
    assert (result.exit_code, result.output) == (0, "")
    assert not is_abandoned(rec.path) and RunRecord.open(tmp_path / "ws", "OLD-1").checkpoint == "C7"


def test_status_shows_abandoned(tmp_path: Path) -> None:
    env, _ = abandon_env(tmp_path)
    stopped(env, "OLD-1", "C7", brief=None)
    abandon(env)
    out = env.invoke("status", "OLD-1", "--config", str(env.config)).output
    assert "state: abandoned" in out and "abandoned_at: 20" in out
    assert "re-enable: " in out and "--again" in out
    assert "awaiting: none" in out and "question:" not in out


def test_digest_notes_abandoned_not_under_needs_you(tmp_path: Path) -> None:
    env, _ = abandon_env(tmp_path)
    rec = stopped(env, "OLD-1", "C7", brief=None)
    abandon(env)
    text = render_digest([RunRecord.open(tmp_path / "ws", "OLD-1")])
    needs = text.split("## Needs you")[1].split("## Done")[0]
    assert "OLD-1" not in needs
    assert "Abandoned: OLD-1" in text and "--again" in text
    assert rec.path.exists()


def test_run_does_not_repick_or_auto_resume_abandoned(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env, _ = abandon_env(tmp_path, ("OLD-1", "NEW-1"))
    stopped(env, "OLD-1", "C1", brief="# B\n\n## Open decisions log\n\nNone\n")  # would auto-resume if waiting
    abandon(env)
    assert is_started(tmp_path / "ws", "OLD-1")
    assert run(env).exit_code == 0
    assert items(fake_run_item) == ["NEW-1"]
    assert "Needs you" in env.digest and "OLD-1" not in env.digest.split("## Done")[0]


def test_abandoned_pr_does_not_block_picking(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    prs = FakePrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env, _ = abandon_env(tmp_path, ("NEW-1",), prs=prs)
    rec = stopped(env, "OLD-1", "C7", brief=None, stage=OPEN_PR_STAGE)
    rec.increment_retry(BRANCH_CREATED_KEY)
    abandon(env)
    assert find_awaiting_review(tmp_path / "ws", prs.find_pr) == []
    assert run(env).exit_code == 0
    assert items(fake_run_item) == ["NEW-1"]


def test_resume_refuses_abandoned_without_again(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env, _ = abandon_env(tmp_path)
    rec = stopped(env, "OLD-1", "C7", brief=None, stage="push")
    abandon(env)
    result = env.invoke("resume", "OLD-1", "--config", str(env.config))
    assert result.exit_code == 1 and "abandoned" in result.output and "--again" in result.output
    assert fake_run_item["calls"] == [] and is_abandoned(rec.path)


def test_resume_again_clears_mark_and_restarts(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env, _ = abandon_env(tmp_path)
    rec = stopped(env, "OLD-1", "C7", brief=None, stage="push")
    abandon(env)
    result = env.invoke("resume", "OLD-1", "--config", str(env.config), "--again")
    assert result.exit_code == 0, result.output
    assert items(fake_run_item) == ["OLD-1"]
    assert not is_abandoned(rec.path)
    assert RunRecord.open(tmp_path / "ws", "OLD-1").stage == "push"  # recorded stage kept


def test_resume_again_on_a_not_abandoned_item_errors(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env, _ = abandon_env(tmp_path)
    stopped(env, "OLD-1", "C5", brief=None)
    result = env.invoke("resume", "OLD-1", "--config", str(env.config), "--again")
    assert result.exit_code == 1 and "not abandoned" in result.output
    assert fake_run_item["calls"] == []


def test_c7_action_points_at_abandon() -> None:
    assert "sisyphus abandon REF --config F" in ACTIONS["C7"]
    assert "rebase the branch yourself" not in ACTIONS["C7"].lower()
