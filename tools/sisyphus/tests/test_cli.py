"""CLI tests: click CliRunner, fake services, a fake ``run_item`` and tmp dirs.

No test runs an agent, the harness, gh, spec-prism-flow or ``git push``.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from sisyphus import cli
from sisyphus.cleanup import PrInfo
from sisyphus.cli import Services, main
from sisyphus.config import ConfigError, WorkItemSource, load_config
from sisyphus.intake import IntakeResult, OpenDecision
from sisyphus.lock import LockHeld, clone_lock
from sisyphus.orchestrator import (
    BRANCH_CREATED_KEY,
    OPEN_PR_STAGE,
    GitHubError,
    PullRequest,
    branch_name,
)
from sisyphus.pick import Candidate, ExecResult, GhCli, GhPullRequestLookup, LinkedPr
from sisyphus.rails import Checkpoint
from sisyphus.runrecord import RunRecord
from sisyphus.stoprecord import build_abandoned_record, build_stop_record, write_stop_record

TODAY = datetime(2026, 1, 2, tzinfo=UTC)


class FakeTracker:
    def __init__(self, refs: Sequence[str] = ()) -> None:
        self.refs = list(refs)
        self.error: Exception | None = None

    def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        if self.error:
            raise self.error
        return [Candidate(r, r, None, "", ("me@x.io",), (), "Todo") for r in self.refs]


class FakeLookup:
    def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        return []

    def open_pr_bodies(self) -> Sequence[str]:
        return []


class FakePrs:
    def __init__(self, open_branches: dict[str, PullRequest] | None = None) -> None:
        self.open = open_branches or {}

    def find_pr(self, branch: str) -> PullRequest | None:
        return self.open.get(branch)

    def pr_state(self, ref: str) -> PrInfo | None:
        return None

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        raise AssertionError("not used")


@dataclass
class Env:
    root: Path
    config: Path
    services: Services

    def invoke(self, *args: str):
        return CliRunner().invoke(main, list(args), obj=lambda _cfg: self.services)

    @property
    def digest(self) -> str:
        return (self.root / "digests" / "2026-01-02.md").read_text()


def make_env(
    tmp_path: Path,
    refs: Sequence[str] = (),
    prs: FakePrs | None = None,
    digest: str | None = None,
    extra: str = "",
    with_digest_section: bool = True,
    agent: bool = True,
) -> Env:
    (tmp_path / "repo").mkdir()
    digest_t = digest if digest is not None else str(tmp_path / "digests" / "<date>.md")
    text = f"""[project]
repo_dir = "{tmp_path / "repo"}"
workspace_dir = "{tmp_path / "ws"}"
brief_path_template = "<workspace_dir>/<item-id>.md"

[source.work_item]
kind = "linear"
assignee = "me@x.io"
pick_state = "Todo"
"""
    if agent:
        text += '\n[agent]\nbackend = "claude"\n'
    if with_digest_section:
        text += f'\n[digest]\npath_template = "{digest_t}"\n'
    cfg = tmp_path / ".sisyphus.toml"
    cfg.write_text(text + extra)
    services = Services(
        tracker=FakeTracker(refs),
        lookup=FakeLookup(),
        pr_client=prs or FakePrs(),
        agent_runner=lambda _p: None,
        lock_dir=tmp_path / "locks",
        today=TODAY,
        repo_info=lambda: "main",
    )
    return Env(tmp_path, cfg, services)


def items(state: dict[str, Any]) -> list[str]:
    return [c[0] for c in state["calls"]]


def test_help_lists_run_and_intake() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "intake" in result.output


def test_nothing_to_do_exits_zero_silently(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path)
    result = env.invoke("run", "--config", str(env.config))
    assert (result.exit_code, result.output) == (0, "")
    assert fake_run_item["calls"] == []
    assert not (tmp_path / "digests").exists()


def test_nothing_to_do_needs_no_base_digest_or_backend(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, with_digest_section=False, agent=False)
    result = env.invoke("run", "--config", str(env.config))
    assert (result.exit_code, result.output) == (0, "")


def test_picks_and_runs_item_with_base_and_writes_digest(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    result = env.invoke("run", "--config", str(env.config), "--base", "feature-base")
    assert result.exit_code == 0, result.output
    ((item, options, kwargs),) = fake_run_item["calls"]
    assert item == "ENG-1"
    assert options.base_branch == "feature-base"
    assert options.default_branch == "main"
    assert options.lock_dir == tmp_path / "locks"
    assert kwargs["write_digest_file"] is False
    assert kwargs["pr_client"] is env.services.pr_client
    assert "ENG-1: draft PR #7" in result.output
    assert "## Needs you" in env.digest


def test_base_flag_wins_over_config(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    env.config.write_text(env.config.read_text().replace("[project]\n", '[project]\nbase_branch = "cfg-base"\n', 1))
    assert env.invoke("run", "--config", str(env.config), "--base", "flag-base").exit_code == 0
    assert fake_run_item["calls"][0][1].base_branch == "flag-base"


def test_config_base_wins_over_main(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    env.config.write_text(env.config.read_text().replace("[project]\n", '[project]\nbase_branch = "cfg-base"\n', 1))
    assert env.invoke("run", "--config", str(env.config)).exit_code == 0
    assert fake_run_item["calls"][0][1].base_branch == "cfg-base"


def test_base_defaults_to_main_when_neither_given(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    assert env.invoke("run", "--config", str(env.config)).exit_code == 0
    assert fake_run_item["calls"][0][1].base_branch == cli.DEFAULT_BASE_BRANCH == "main"


def test_max_tickets_per_run_caps_picks(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["A-1", "A-2", "A-3"], extra="\n[limits]\nmax_tickets_per_run = 2\n")
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0
    assert items(fake_run_item) == ["A-1", "A-2"]
    assert len(list((tmp_path / "digests").iterdir())) == 1


def test_default_is_one_ticket(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["A-1", "A-2"])
    env.invoke("run", "--config", str(env.config), "--base", "b")
    assert items(fake_run_item) == ["A-1"]


def test_started_item_is_skipped(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["A-1", "A-2"])
    RunRecord.open(tmp_path / "ws", "A-1")
    env.invoke("run", "--config", str(env.config), "--base", "b")
    assert items(fake_run_item) == ["A-2"]


def test_max_run_minutes_stops_before_next_item(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["A-1", "A-2"], extra="\n[limits]\nmax_tickets_per_run = 2\nmax_run_minutes = 1\n")
    now = [0.0]
    env.services.clock = lambda: now[0]
    fake_run_item["hook"] = lambda _c, _o: now.__setitem__(0, 120.0)
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0
    assert items(fake_run_item) == ["A-1"]
    assert "max_run_minutes (1) reached before starting A-2" in env.digest


def test_open_pr_of_earlier_item_does_not_block_pick(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    prs = FakePrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")})
    env = make_env(tmp_path, ["NEW-1"], prs=prs)
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    rec.mark_stage(OPEN_PR_STAGE, "complete")
    rec.increment_retry(BRANCH_CREATED_KEY)
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0
    assert items(fake_run_item) == ["NEW-1"]
    assert "awaiting-review" not in env.digest
    assert "awaits review" not in env.digest


def test_status_of_finished_item_with_open_pr_is_none_without_pr_lookup(tmp_path: Path) -> None:
    looked_up: list[str] = []

    class SpyPrs(FakePrs):
        def find_pr(self, branch: str) -> PullRequest | None:
            looked_up.append(branch)
            return super().find_pr(branch)

    env = make_env(tmp_path, prs=SpyPrs({branch_name("OLD-1"): PullRequest(3, "http://pr/3")}))
    rec = RunRecord.open(tmp_path / "ws", "OLD-1")
    rec.mark_stage(OPEN_PR_STAGE, "complete")
    rec.increment_retry(BRANCH_CREATED_KEY)
    result = env.invoke("status", "--all", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert "awaiting: none" in result.output
    assert looked_up == []


def _status_workspace(tmp_path: Path) -> Any:
    """An env with one record per status case: abandoned, finished, checkpoint, error stop, in progress."""
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    abandoned = RunRecord.open(ws, "AB-1")
    abandoned.mark_stage("build")
    write_stop_record(
        abandoned.path,
        build_abandoned_record(
            "AB-1", stage="build", stage_status="in_progress", prior_label="l", reenable_command="r"
        ),
    )
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    fin_err = RunRecord.open(ws, "FIN-ERR-1")
    fin_err.mark_stage(OPEN_PR_STAGE, "complete")
    write_stop_record(
        fin_err.path,
        build_stop_record(
            "FIN-ERR-1",
            checkpoint=None,
            stage=OPEN_PR_STAGE,
            stage_status="complete",
            detail="x",
            resume_command="r",
        ),
    )
    fin_cp = RunRecord.open(ws, "FIN-CP-1")
    fin_cp.mark_stage(OPEN_PR_STAGE, "complete")
    fin_cp.set_checkpoint("C1")
    # Not reachable through normal operation (abandoning clears the checkpoint); built directly to pin 7.3's
    # checkpoint-first precedence in `_is_settled` and the awaiting label.
    ab_cp = RunRecord.open(ws, "AB-CP-1")
    ab_cp.mark_stage("build")
    ab_cp.set_checkpoint("C1")
    write_stop_record(
        ab_cp.path,
        build_abandoned_record(
            "AB-CP-1", stage="build", stage_status="in_progress", prior_label="l", reenable_command="r"
        ),
    )
    cp = RunRecord.open(ws, "CP-1")
    cp.mark_stage("plan")
    cp.set_checkpoint("C1")
    err = RunRecord.open(ws, "ERR-1")
    err.mark_stage("build")
    write_stop_record(
        err.path,
        build_stop_record(
            "ERR-1", checkpoint=None, stage="build", stage_status="in_progress", detail="x", resume_command="r"
        ),
    )
    RunRecord.open(ws, "RUN-1").mark_stage("build", "in_progress")
    return env


def _shown(output: str) -> list[str]:
    return [line for line in output.splitlines() if line and not line.startswith((" ", "no items"))]


def _awaiting(output: str) -> dict[str, str]:
    found: dict[str, str] = {}
    item = ""
    for line in output.splitlines():
        if line and not line.startswith(" "):
            item = line
        elif line.strip().startswith("awaiting: "):
            found[item] = line.strip().removeprefix("awaiting: ")
    return found


def test_status_hides_settled_items_by_default(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    result = env.invoke("status", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert sorted(_shown(result.output)) == ["AB-CP-1", "CP-1", "ERR-1", "FIN-CP-1", "FIN-ERR-1", "RUN-1"]
    assert _awaiting(result.output) == {
        "AB-CP-1": "you",
        "CP-1": "you",
        "ERR-1": "error",
        "FIN-CP-1": "you",
        "FIN-ERR-1": "error",
        "RUN-1": "none",
    }
    assert "no items needing attention" not in result.output


def test_status_all_shows_every_item(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    result = env.invoke("status", "--all", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert sorted(_shown(result.output)) == [
        "AB-1",
        "AB-CP-1",
        "CP-1",
        "ERR-1",
        "FIN-1",
        "FIN-CP-1",
        "FIN-ERR-1",
        "RUN-1",
    ]
    awaiting = _awaiting(result.output)
    assert (awaiting["FIN-ERR-1"], awaiting["FIN-CP-1"], awaiting["AB-CP-1"]) == ("error", "you", "you")


def test_status_explicit_ref_shows_settled_item_without_all(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    for ref in ("AB-1", "FIN-1"):
        result = env.invoke("status", ref, "--config", str(env.config))
        assert result.exit_code == 0, result.output
        assert _shown(result.output) == [ref]
    missing = env.invoke("status", "NOPE-1", "--config", str(env.config))
    assert missing.exit_code == 1
    assert "no run record for NOPE-1" in missing.output


def test_status_all_hidden_reports_count_and_empty_says_no_items(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    empty = env.invoke("status", "--config", str(env.config))
    assert (empty.exit_code, empty.output.strip()) == (0, "no items")
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    RunRecord.open(ws, "FIN-2").mark_stage(OPEN_PR_STAGE, "complete")
    result = env.invoke("status", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "no items needing attention (2 hidden; use --all)"


def test_lock_held_exits_zero_and_orchestrator_not_called(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert (result.exit_code, result.output) == (0, "")
    assert fake_run_item["calls"] == []


def _is_locked(tmp_path: Path) -> bool:
    """True when a second open cannot take the clone lock right now."""
    try:
        with clone_lock(tmp_path / "repo", tmp_path / "locks"):
            return False
    except LockHeld:
        return True


def test_lock_held_for_whole_invocation_with_no_gap(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1", "ENG-2", "ENG-3"], extra="\n[limits]\nmax_tickets_per_run = 3\n")
    seen: dict[str, list[bool]] = {"pick": [], "items": []}
    real_pick = cli.pick_items

    def pick(*a: Any, **k: Any) -> Any:
        seen["pick"].append(_is_locked(tmp_path))
        return real_pick(*a, **k)

    fake_run_item["hook"] = lambda _c, _o: seen["items"].append(_is_locked(tmp_path))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "pick_items", pick)
        result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0, result.output
    assert items(fake_run_item) == ["ENG-1", "ENG-2", "ENG-3"]
    assert seen == {"pick": [True], "items": [True, True, True]}
    assert all(c[2]["lock_held"] is True for c in fake_run_item["calls"])
    assert not _is_locked(tmp_path)


def test_lock_held_while_writing_digest(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    seen: list[bool] = []
    real = cli.write_digest

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(_is_locked(tmp_path))
        return real(*a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "write_digest", spy)
        env.invoke("run", "--config", str(env.config), "--base", "b")
    assert seen == [True]


def test_lock_released_after_error(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    fake_run_item["raise"]["ENG-1"] = RuntimeError("boom")
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 1
    assert not _is_locked(tmp_path)


def test_lock_released_after_success(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    env.invoke("run", "--config", str(env.config), "--base", "b")
    assert not _is_locked(tmp_path)


def test_lock_held_means_no_pick_or_run(
    tmp_path: Path, fake_run_item: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    touched: list[str] = []
    monkeypatch.setattr(cli, "pick_items", lambda *a, **k: touched.append("p") or [])
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert (result.exit_code, result.output) == (0, "")
    assert touched == []
    assert fake_run_item["calls"] == []


def test_checkpoint_stop_exits_zero_with_digest(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    fake_run_item["checkpoint"]["ENG-1"] = Checkpoint.C3
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0
    assert "stopped at C3" in result.output
    assert "[C3] ENG-1" in env.digest


def test_unhandled_error_is_nonzero_and_digest_still_written(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    fake_run_item["raise"]["ENG-1"] = RuntimeError("agent exploded")
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 1
    assert "agent exploded" in result.output
    assert "ENG-1: run failed: agent exploded" in env.digest


def test_tracker_failure_is_nonzero_and_digest_written(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    tracker = FakeTracker(["ENG-1"])
    tracker.error = RuntimeError("tracker down")
    env.services.tracker = tracker
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 1
    assert "tracker down" in result.output
    assert "tracker down" in env.digest


def test_digest_write_failure_is_nonzero_with_stderr(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    env = make_env(tmp_path, ["ENG-1"], digest=str(blocker / "d.md"))
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 1
    assert "cannot write digest" in result.stderr


def test_config_error_is_nonzero(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[nope]\n")
    result = CliRunner().invoke(main, ["run", "--config", str(bad)])
    assert result.exit_code == 1
    assert "config error" in result.stderr
    missing = CliRunner().invoke(main, ["run", "--config", str(tmp_path / "missing.toml")])
    assert missing.exit_code == 1
    assert "not found" in missing.stderr


def test_missing_backend_when_work_exists(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"], agent=False)
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 1
    assert "backend is not set" in result.output
    assert fake_run_item["calls"] == []


# ----------------------------------------------------------------- intake


def test_intake_runs_intake_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path)
    seen: dict[str, Any] = {}

    def fake_intake(item, config, runner, record=None, **_kw):
        seen.update(item=item, record=record)
        return IntakeResult(tmp_path / "b.md", False, (OpenDecision(1, "Which tracker?"),))

    monkeypatch.setattr(cli, "run_intake", fake_intake)
    result = env.invoke("intake", "ENG-9", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert seen["item"] == "ENG-9"
    assert seen["record"].item == "ENG-9"
    assert f"brief: {tmp_path / 'b.md'}" in result.output
    assert "Which tracker?" in result.output


def test_intake_lock_held_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path)

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("bad brief")

    monkeypatch.setattr(cli, "run_intake", boom)
    failed = env.invoke("intake", "ENG-9", "--config", str(env.config))
    assert failed.exit_code == 1
    assert "bad brief" in failed.output
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        held = env.invoke("intake", "ENG-9", "--config", str(env.config))
    assert (held.exit_code, held.output) == (0, "")
    assert not _is_locked(tmp_path)


# ------------------------------------------------------ real service builders


class Proc:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


def test_agent_runner_commands(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> Proc:
        calls.append((argv, kw))
        return Proc(0)

    cli.make_agent_runner("claude", tmp_path, fake_run)("do it")
    cli.make_agent_runner("opencode", tmp_path, fake_run)("do it")
    assert calls[0][0] == ["claude", "-p", "do it", "--dangerously-skip-permissions"]
    assert calls[1][0] == ["opencode", "run", "do it"]
    assert calls[0][1]["cwd"] == tmp_path


def test_agent_runner_failures(tmp_path: Path) -> None:
    with pytest.raises(cli.AgentError, match="status 3"):
        cli.make_agent_runner("claude", tmp_path, lambda *_a, **_k: Proc(3))("x")

    def missing(*_a: Any, **_k: Any) -> Proc:
        raise FileNotFoundError("claude")

    with pytest.raises(cli.AgentError, match="cannot start"):
        cli.make_agent_runner("opencode", tmp_path, missing)("x")
    with pytest.raises(ConfigError):
        cli.make_agent_runner(None, tmp_path)


def test_token_cmd() -> None:
    assert cli.run_token_cmd(None) is None
    seen: list[list[str]] = []

    def ok(argv: list[str], **_k: Any) -> Proc:
        seen.append(argv)
        return Proc(0, "  tok\n")

    assert cli.run_token_cmd("gh auth token --user me", ok) == "tok"
    assert seen == [["gh", "auth", "token", "--user", "me"]]
    with pytest.raises(ConfigError):
        cli.run_token_cmd("x", lambda *_a, **_k: Proc(1, ""))
    with pytest.raises(ConfigError):
        cli.run_token_cmd("x", lambda *_a, **_k: Proc(0, "  "))

    def timeout(*_a: Any, **_k: Any) -> Proc:
        raise subprocess.TimeoutExpired("x", 1)

    with pytest.raises(ConfigError):
        cli.run_token_cmd("x", timeout)


def test_gh_pull_request_client() -> None:
    replies = [
        ExecResult(0, json.dumps([{"number": 4, "url": "u"}])),
        ExecResult(0, "[]"),
        ExecResult(0, "https://github.com/o/r/pull/12\n"),
        ExecResult(1, "", "no auth"),
        ExecResult(1, "", "no auth"),
    ]
    calls: list[list[str]] = []

    def ex(argv: Sequence[str], _cwd: Path | None, _env: Any) -> ExecResult:
        calls.append(list(argv))
        return replies.pop(0)

    client = cli.GhPullRequestClient(GhCli(None, None, ex))
    assert client.find_pr("b") == PullRequest(4, "u")
    assert client.find_pr("b") is None
    assert client.open_pr("b", "base", "T", "B") == PullRequest(12, "https://github.com/o/r/pull/12")
    assert calls[2][:3] == ["gh", "pr", "create"]
    assert "--draft" in calls[2]
    assert "base" in calls[2]
    with pytest.raises(GitHubError):
        client.open_pr("b", "base", "T", "B")
    with pytest.raises(GitHubError):
        client.find_pr("b")


def test_draft_pr_is_still_found_and_reported_open() -> None:
    replies = [
        ExecResult(0, json.dumps([{"number": 9, "url": "u9", "isDraft": True}])),
        ExecResult(0, '{"number": 9, "state": "OPEN", "isDraft": true, "headRefOid": "abc"}'),
    ]

    def ex(_argv: Sequence[str], _cwd: Path | None, _env: Any) -> ExecResult:
        return replies.pop(0)

    client = cli.GhPullRequestClient(GhCli(None, None, ex))
    assert client.find_pr("b") == PullRequest(9, "u9")
    assert client.pr_state("9") == PrInfo(9, "OPEN", "abc")


def test_gh_client_pr_state_reads_state_and_head_oid() -> None:
    replies = [
        ExecResult(0, '{"number": 7, "state": "MERGED", "headRefOid": "abc"}'),
        ExecResult(1, "", "no pull requests found"),
    ]
    calls: list[list[str]] = []

    def ex(argv: Sequence[str], _cwd: Path | None, _env: Any) -> ExecResult:
        calls.append(list(argv))
        return replies.pop(0)

    client = cli.GhPullRequestClient(GhCli(None, None, ex))
    assert client.pr_state("7") == PrInfo(7, "MERGED", "abc")
    assert calls[0] == ["gh", "pr", "view", "7", "--json", "number,state,headRefOid"]
    with pytest.raises(GitHubError):
        client.pr_state("b")


def test_default_services_builds_clients(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    env = make_env(tmp_path)
    services = cli.default_services(load_config(env.config))
    assert isinstance(services.lookup, GhPullRequestLookup)
    assert isinstance(services.pr_client, cli.GhPullRequestClient)
    assert services.repo_info is not None


def test_default_services_file_kind_builds_file_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    env = make_env(tmp_path)
    text = env.config.read_text().replace(
        'kind = "linear"\nassignee = "me@x.io"\npick_state = "Todo"', f'kind = "file"\ndir = "{tmp_path / "inbox"}"'
    )
    env.config.write_text(text)
    services = cli.default_services(load_config(env.config))
    assert isinstance(services.tracker, cli.FileTracker)


def test_no_harness_or_spec_prism_flow_import() -> None:
    tree = ast.parse(Path(cli.__file__).read_text())
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(n.split(".")[0] in ("harness", "spec_prism_flow") for n in names)


def test_default_path_without_linear_key_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    env = make_env(tmp_path)
    result = CliRunner().invoke(main, ["run", "--config", str(env.config)])
    assert result.exit_code == 1
    assert "LINEAR_API_KEY" in result.stderr


def test_agent_runner_timeout(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def hung(argv: list[str], **kw: Any) -> Proc:
        seen.update(kw)
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    with pytest.raises(cli.AgentError, match="timed out after 2 minutes"):
        cli.make_agent_runner("claude", tmp_path, hung, timeout_minutes=2)("x")
    assert seen["timeout"] == 120
