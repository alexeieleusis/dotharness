"""CLI tests: typer CliRunner, fake services, a fake ``run_item`` and tmp dirs.

No test runs an agent, the harness, gh, spec-prism-flow or ``git push``.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, RunResult
from dotharness.code_host import (
    Capability,
    ChangeRequestState,
    CodeHostError,
    ConfigurationError,
    UnsupportedCapabilityError,
)
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.sisyphus import cli
from dotharness.sisyphus.agent import AgentError, make_agent_runner
from dotharness.sisyphus.cleanup import PrInfo
from dotharness.sisyphus.cli import Services, app
from dotharness.sisyphus.config import ConfigError, SisyphusConfig, WorkItemSource, load_config
from dotharness.sisyphus.intake import IntakeResult, OpenDecision
from dotharness.sisyphus.lock import LockHeld, clone_lock
from dotharness.sisyphus.orchestrator import (
    BRANCH_CREATED_KEY,
    GATE_FAILED_KEY,
    OPEN_PR_STAGE,
    PR_NUMBER_KEY,
    GitHubError,
    PullRequest,
    branch_name,
)
from dotharness.sisyphus.pick import (
    Candidate,
    HostIssueTracker,
    HostLabelRemover,
    HostPullRequestLookup,
    HostRepository,
    LinkedPr,
)
from dotharness.sisyphus.rails import Checkpoint
from dotharness.sisyphus.runrecord import RunRecord, record_path
from dotharness.sisyphus.stoprecord import build_abandoned_record, build_stop_record, write_stop_record

TODAY = datetime(2026, 1, 2, tzinfo=UTC)


async def _noop_agent(_prompt: str) -> None:
    return None


async def _main_branch() -> str:
    return "main"


class FakeTracker:
    def __init__(self, refs: Sequence[str] = ()) -> None:
        self.refs = list(refs)
        self.error: Exception | None = None

    async def list_candidates(self, source: WorkItemSource) -> Sequence[Candidate]:
        if self.error:
            raise self.error
        return [Candidate(r, r, None, "", ("me@x.io",), (), "Todo") for r in self.refs]


class FakeLookup:
    async def linked_prs(self, issue_number: int) -> Sequence[LinkedPr]:
        return []

    async def open_pr_bodies(self) -> Sequence[str]:
        return []


class FakePrs:
    def __init__(self, open_branches: dict[str, PullRequest] | None = None) -> None:
        self.open = open_branches or {}

    async def find_pr(self, branch: str) -> PullRequest | None:
        return self.open.get(branch)

    async def pr_state(self, ref: str) -> PrInfo | None:
        return None

    async def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        raise AssertionError("not used")


@dataclass
class Env:
    root: Path
    config: Path
    services: Services

    def invoke(self, *args: str):
        return CliRunner().invoke(app, list(args), obj=lambda _cfg: self.services)

    @property
    def digest(self) -> str:
        return (self.root / "digests" / "2026-01-02.md").read_text()


class FakeLabelRemover:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def remove_label(self, number: int, label: str) -> None:
        self.calls.append((number, label))


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
        label_remover=FakeLabelRemover(),
        agent_runner=_noop_agent,
        lock_dir=tmp_path / "locks",
        today=TODAY,
        repo_info=_main_branch,
    )
    return Env(tmp_path, cfg, services)


def items(state: dict[str, Any]) -> list[str]:
    return [c[0] for c in state["calls"]]


def test_help_lists_run_and_intake() -> None:
    result = CliRunner().invoke(app, ["--help"])
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
    assert kwargs["label_remover"] is env.services.label_remover
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
        async def find_pr(self, branch: str) -> PullRequest | None:
            looked_up.append(branch)
            return await super().find_pr(branch)

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
    # Legacy `ci-fix` stage (dropped in #129) still loads from old run records.
    RunRecord.open(ws, "LEG-1").mark_stage("ci-fix", "complete")
    leg_cp = RunRecord.open(ws, "LEG-CP-1")
    leg_cp.mark_stage("ci-fix", "complete")
    leg_cp.set_checkpoint("C1")
    leg_err = RunRecord.open(ws, "LEG-ERR-1")
    leg_err.mark_stage("ci-fix", "complete")
    write_stop_record(
        leg_err.path,
        build_stop_record(
            "LEG-ERR-1", checkpoint=None, stage="ci-fix", stage_status="complete", detail="x", resume_command="r"
        ),
    )
    leg_ab = RunRecord.open(ws, "LEG-AB-1")
    leg_ab.mark_stage("ci-fix", "complete")
    write_stop_record(
        leg_ab.path,
        build_abandoned_record(
            "LEG-AB-1", stage="ci-fix", stage_status="complete", prior_label="l", reenable_command="r"
        ),
    )
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
    # PR-state cases (`--check-prs`): C5-stopped at open-pr with a recorded PR, plus the ones that must stay shown.
    for ref, number in (("PR-MERGED-1", 11), ("PR-CLOSED-1", 12), ("PR-OPEN-1", 13), ("PR-FAIL-1", 14)):
        pr_rec = RunRecord.open(ws, ref)
        pr_rec.mark_stage(OPEN_PR_STAGE, "in_progress")
        pr_rec.retries[PR_NUMBER_KEY] = number
        pr_rec.set_checkpoint("C5")
        write_stop_record(
            pr_rec.path,
            build_stop_record(
                ref, checkpoint="C5", stage=OPEN_PR_STAGE, stage_status="in_progress", detail="x", resume_command="r"
            ),
        )
    pr_unknown = RunRecord.open(ws, "PR-UNKNOWN-1")  # checkpoint, no recorded PR number
    pr_unknown.mark_stage(OPEN_PR_STAGE, "in_progress")
    pr_unknown.set_checkpoint("C5")
    pr_run = RunRecord.open(ws, "PR-RUN-1")  # in progress, no stop record, PR already merged
    pr_run.mark_stage("build", "in_progress")
    pr_run.retries[PR_NUMBER_KEY] = 15
    pr_run.save()
    return env


PR_STATES = {11: "MERGED", 12: "CLOSED", 13: "OPEN", 15: "MERGED"}
PR_SHOWN = ["PR-FAIL-1", "PR-OPEN-1", "PR-RUN-1", "PR-UNKNOWN-1"]


def _spy_pr_state(env: Any) -> list[int]:
    looked_up: list[int] = []

    async def pr_state(number: int) -> str:
        looked_up.append(number)
        if number not in PR_STATES:
            raise CodeHostError(f"gh pr view failed (exit 1): no PR {number}")
        return PR_STATES[number]

    env.services.pr_state = pr_state
    return looked_up


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


PR_RECORDS = ["PR-CLOSED-1", "PR-FAIL-1", "PR-MERGED-1", "PR-OPEN-1", "PR-RUN-1", "PR-UNKNOWN-1"]


def test_status_hides_settled_items_by_default(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    looked_up = _spy_pr_state(env)
    result = env.invoke("status", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert sorted(_shown(result.output)) == [
        "AB-CP-1",
        "CP-1",
        "ERR-1",
        "FIN-CP-1",
        "FIN-ERR-1",
        "LEG-CP-1",
        "LEG-ERR-1",
        *PR_RECORDS,
        "RUN-1",
    ]
    assert _awaiting(result.output) == {
        "AB-CP-1": "you",
        "CP-1": "you",
        "ERR-1": "error",
        "FIN-CP-1": "you",
        "FIN-ERR-1": "error",
        "LEG-CP-1": "you",
        "LEG-ERR-1": "error",
        "PR-CLOSED-1": "you",
        "PR-FAIL-1": "you",
        "PR-MERGED-1": "you",
        "PR-OPEN-1": "you",
        "PR-RUN-1": "none",
        "PR-UNKNOWN-1": "you",
        "RUN-1": "none",
    }
    assert "no items needing attention" not in result.output
    assert looked_up == []  # no network without --check-prs


def test_status_check_prs_hides_merged_and_closed_and_fails_open(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    looked_up = _spy_pr_state(env)
    result = env.invoke("status", "--check-prs", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    shown = _shown(result.output)
    assert "PR-MERGED-1" not in shown
    assert "PR-CLOSED-1" not in shown
    # open PR, failing lookup, no recorded PR and in-progress-without-stop stay shown
    assert set(PR_SHOWN) <= set(shown)
    # one lookup per candidate; the in-progress record without a stop is never looked up
    assert sorted(looked_up) == [11, 12, 13, 14]
    assert "cannot check PR #14 for PR-FAIL-1" in result.stderr
    assert result.stderr.count("warning:") == 1


def test_status_check_prs_hidden_count_includes_pr_state_records(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    for ref, number in (("PR-MERGED-1", 11), ("PR-CLOSED-1", 12)):
        rec = RunRecord.open(ws, ref)
        rec.mark_stage(OPEN_PR_STAGE, "in_progress")
        rec.retries[PR_NUMBER_KEY] = number
        rec.set_checkpoint("C5")
    _spy_pr_state(env)
    result = env.invoke("status", "--check-prs", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "no items needing attention (3 hidden; use --all)"


def test_status_check_prs_skipped_for_all_and_explicit_ref(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    looked_up = _spy_pr_state(env)
    all_result = env.invoke("status", "--all", "--check-prs", "--config", str(env.config))
    assert all_result.exit_code == 0, all_result.output
    assert {"PR-MERGED-1", "PR-CLOSED-1"} <= set(_shown(all_result.output))
    ref_result = env.invoke("status", "PR-MERGED-1", "--check-prs", "--config", str(env.config))
    assert ref_result.exit_code == 0, ref_result.output
    assert _shown(ref_result.output) == ["PR-MERGED-1"]
    assert looked_up == []


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
        "LEG-1",
        "LEG-AB-1",
        "LEG-CP-1",
        "LEG-ERR-1",
        *PR_RECORDS,
        "RUN-1",
    ]
    awaiting = _awaiting(result.output)
    assert (awaiting["FIN-ERR-1"], awaiting["FIN-CP-1"], awaiting["AB-CP-1"]) == ("error", "you", "you")


def test_status_explicit_ref_shows_settled_item_without_all(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    for ref in ("AB-1", "FIN-1", "LEG-1", "LEG-AB-1"):
        result = env.invoke("status", ref, "--config", str(env.config))
        assert result.exit_code == 0, result.output
        assert _shown(result.output) == [ref]
    missing = env.invoke("status", "NOPE-1", "--config", str(env.config))
    assert missing.exit_code == 1
    assert "no run record for NOPE-1" in missing.output


def test_status_hides_finished_item_with_failed_gate_by_default_but_all_and_ref_show_it(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    failed = RunRecord.open(ws, "GATE-1")
    failed.mark_stage(OPEN_PR_STAGE, "complete")
    failed.retries[GATE_FAILED_KEY] = 1
    failed.save()
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    default = env.invoke("status", "--config", str(env.config))
    assert default.exit_code == 0, default.output
    assert default.output.strip() == "no items needing attention (2 hidden; use --all)"
    shown_all = env.invoke("status", "--all", "--config", str(env.config))
    assert sorted(_shown(shown_all.output)) == ["FIN-1", "GATE-1"]
    ref = env.invoke("status", "GATE-1", "--config", str(env.config))
    assert ref.exit_code == 0, ref.output
    assert _shown(ref.output) == ["GATE-1"]


def test_status_all_hidden_reports_count_and_empty_says_no_items(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    empty = env.invoke("status", "--config", str(env.config))
    assert (empty.exit_code, empty.output.strip()) == (0, "no items")
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    RunRecord.open(ws, "FIN-2").mark_stage(OPEN_PR_STAGE, "complete")
    RunRecord.open(ws, "LEG-1").mark_stage("ci-fix", "complete")
    result = env.invoke("status", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "no items needing attention (3 hidden; use --all)"


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

    async def fake_pick(*_a: Any, **_k: Any) -> list[Any]:
        touched.append("p")
        return []

    monkeypatch.setattr(cli, "pick_items", fake_pick)
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
    result = CliRunner().invoke(app, ["run", "--config", str(bad)])
    assert result.exit_code == 1
    assert "config error" in result.stderr
    missing = CliRunner().invoke(app, ["run", "--config", str(tmp_path / "missing.toml")])
    assert missing.exit_code == 1
    assert "not found" in missing.stderr


def test_missing_backend_when_work_exists(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"], agent=False)
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 1
    assert "backend is not set" in result.output
    assert result.stderr.count("Error:") == 1
    assert fake_run_item["calls"] == []


# ----------------------------------------------------------------- intake


def test_intake_runs_intake_stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path)
    seen: dict[str, Any] = {}

    async def fake_intake(item, config, runner, record=None, **_kw):
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


def _result(returncode: int = 0, *, timed_out: bool = False, not_found: bool = False, stderr: str = "") -> RunResult:
    return RunResult(returncode, "", stderr, timed_out=timed_out, not_found=not_found)


async def test_agent_runner_launch_arguments(tmp_path: Path) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_run(*args: Any, **kw: Any) -> RunResult:
        calls.append((args, kw))
        return _result()

    await make_agent_runner("claude", tmp_path, fake_run, timeout_minutes=3)("do it")
    await make_agent_runner("opencode", tmp_path, fake_run, timeout_minutes=3)("do it")
    assert calls[0][0] == ("claude", "do it")
    assert calls[0][1]["backend_options"] == ClaudeOptions(skip_permissions=True)
    assert calls[1][0] == ("opencode", "do it")
    assert calls[1][1]["backend_options"] == OpencodeOptions()  # no --standalone (D-4)
    assert calls[0][1]["cwd"] == tmp_path
    assert calls[0][1]["timeout"] == 180


async def test_agent_runner_failures(tmp_path: Path) -> None:
    def runner(result: RunResult) -> Any:
        async def launch(*_a: Any, **_k: Any) -> RunResult:
            return result

        return make_agent_runner("claude", tmp_path, launch, timeout_minutes=1)

    with pytest.raises(AgentError, match="the claude backend exited with status 3"):
        await runner(_result(3))("x")
    with pytest.raises(AgentError, match="cannot start the claude backend: no such file"):
        await runner(_result(127, not_found=True, stderr="no such file"))("x")
    with pytest.raises(AgentError, match="the claude backend timed out after 1 minutes"):
        await runner(_result(124, timed_out=True))("x")
    with pytest.raises(ConfigError):
        make_agent_runner(None, tmp_path, timeout_minutes=1)


def test_agent_runner_requires_timeout(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        make_agent_runner("claude", tmp_path)  # ty: ignore[missing-argument]


def _host_config(tmp_path: Path, host_toml: str) -> Any:
    tmp_path.mkdir(exist_ok=True)
    env = make_env(tmp_path)
    env.config.write_text(env.config.read_text() + "\n[source.host]\n" + host_toml)
    return load_config(env.config)


def test_create_host_no_token_cmd(tmp_path: Path) -> None:
    host, token = cli.create_host(_host_config(tmp_path, ""), tmp_path)
    assert token is None
    assert host is not None


def test_create_host_runs_token_cmd_eagerly(tmp_path: Path) -> None:
    cfg = _host_config(tmp_path, f"token_cmd = \"{sys.executable} -c 'print(1234)'\"\n")
    _host, token = cli.create_host(cfg, tmp_path)
    assert token == "1234"  # noqa: S105


def test_create_host_token_cmd_failure_text(tmp_path: Path) -> None:
    cfg = _host_config(tmp_path, f"token_cmd = \"{sys.executable} -c 'import sys; sys.exit(3)'\"\n")
    with pytest.raises(ConfigError) as ei:
        cli.create_host(cfg, tmp_path)
    assert str(ei.value) == "[source.host] token_cmd failed (exit 3) or printed no token"
    empty = _host_config(tmp_path / "second", f'token_cmd = "{sys.executable} -c pass"\n')
    with pytest.raises(ConfigError, match=r"failed \(exit 0\) or printed no token"):
        cli.create_host(empty, tmp_path)


def test_create_host_token_cmd_cannot_run_hides_argv(tmp_path: Path) -> None:
    cfg = _host_config(tmp_path, 'token_cmd = "/nonexistent/s3cret-prog"\n')
    with pytest.raises(ConfigError, match=r"failed to run \(OSError\)") as ei:
        cli.create_host(cfg, tmp_path)
    assert "s3cret" not in str(ei.value)


def test_create_host_unknown_name(tmp_path: Path) -> None:
    cfg = _host_config(tmp_path, 'name = "nope"\n')
    with pytest.raises(ConfigError) as ei:
        cli.create_host(cfg, tmp_path)
    assert str(ei.value) == 'code host setting [source.host] name = "nope": host is not found'


def test_create_host_options_without_name(tmp_path: Path) -> None:
    cfg = _host_config(tmp_path, "[source.host.options]\nx = 1\n")
    with pytest.raises(
        ConfigError, match=r'name = "<unset>": options are set but \[source.host\] name has no host name'
    ):
        cli.create_host(cfg, tmp_path)


def test_default_services_unknown_host_exits_1(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.config.write_text(env.config.read_text() + '\n[source.host]\nname = "nope"\n')
    cfg = load_config(env.config)
    with pytest.raises(ConfigError, match="code host setting"):
        cli.default_services(cfg)


def _client(**kwargs: Any) -> tuple[InMemoryCodeHost, cli.HostPullRequestClient, HostRepository]:
    fake = InMemoryCodeHost(**kwargs)
    repo = HostRepository(fake.code_host())
    return fake, cli.HostPullRequestClient(repo), repo


async def test_host_pull_request_client_find_and_open() -> None:
    fake, client, repo = _client()
    ref = await repo.ref()
    assert await client.find_pr("b") is None
    opened = await client.open_pr("b", "base", "T", "B")
    created = fake.change_requests[ref][0]
    assert opened == PullRequest(created.number, created.url)
    assert (created.head_reference, created.base_reference, created.title, created.body, created.is_draft) == (
        "b", "base", "T", "B", True
    )  # fmt: skip
    assert await client.find_pr("b") == opened
    fake.fail_next("ChangeRequests.create_draft", CodeHostError("exit 1: no auth"))
    with pytest.raises(GitHubError, match="no auth"):
        await client.open_pr("c", "base", "T", "B")
    fake.fail_next("ChangeRequests.find_by_branch", CodeHostError("exit 1: no auth"))
    with pytest.raises(GitHubError):
        await client.find_pr("b")


async def test_host_client_draft_pr_is_found_and_reported_open() -> None:
    fake, client, repo = _client()
    cr = fake.add_change_request(await repo.ref(), head_reference="b", is_draft=True, head_revision="abc")
    assert await client.find_pr("b") == PullRequest(cr.number, cr.url)
    assert await client.pr_state(str(cr.number)) == PrInfo(cr.number, "OPEN", "abc")


async def test_host_client_pr_state_by_number_or_branch_any_state() -> None:
    fake, client, repo = _client()
    ref = await repo.ref()
    fake.add_change_request(ref, head_reference="old", state=ChangeRequestState.CLOSED, head_revision="c1")
    merged = fake.add_change_request(ref, head_reference="b", state=ChangeRequestState.MERGED, head_revision="abc")
    assert await client.pr_state(str(merged.number)) == PrInfo(merged.number, "MERGED", "abc")
    assert await client.pr_state("b") == PrInfo(merged.number, "MERGED", "abc")  # a branch ref sees any state
    assert await client.pr_state("nobranch") is None  # an empty result is None, not an error
    with pytest.raises(GitHubError):
        await client.pr_state("99")  # a missing number raises; cleanup keeps the branch with a note


async def test_host_client_without_change_requests_raises() -> None:
    _fake, client, _repo = _client(capabilities=frozenset(Capability) - {Capability.CHECKS, Capability.CHANGE_REQUESTS})
    for call in (client.find_pr("b"), client.open_pr("b", "m", "T", "B"), client.pr_state("b")):
        with pytest.raises(UnsupportedCapabilityError):
            await call


def _fake_host_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_fake: InMemoryCodeHost) -> cli.Services:
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    env = make_env(tmp_path)
    monkeypatch.setattr(cli, "create_host", lambda _config, _repo_dir: (host_fake.code_host(), None))
    return cli.default_services(load_config(env.config))


def _github_config(tmp_path: Path) -> SisyphusConfig:
    env = make_env(tmp_path)
    text = env.config.read_text().replace(
        'kind = "linear"\nassignee = "me@x.io"\npick_state = "Todo"',
        'kind = "github"\nassignee = "me"\npick_label = "bot"',
    )
    env.config.write_text(text)
    return load_config(env.config)


def test_default_services_wires_the_created_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    services = _fake_host_services(tmp_path, monkeypatch, InMemoryCodeHost())
    assert isinstance(services.lookup, HostPullRequestLookup)
    assert isinstance(services.pr_client, cli.HostPullRequestClient)
    assert isinstance(services.label_remover, HostLabelRemover)
    assert services.repo_info is not None
    assert services.pr_state is not None


async def test_default_services_repo_info_and_pr_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = InMemoryCodeHost()
    fake.set_default_branch("trunk")
    services = _fake_host_services(tmp_path, monkeypatch, fake)
    assert services.repo_info is not None and services.pr_state is not None
    assert await services.repo_info() == "trunk"
    repo_ref = await fake.code_host().repository.current()
    cr = fake.add_change_request(repo_ref, head_reference="b", state=ChangeRequestState.MERGED)
    assert await services.pr_state(cr.number) == "MERGED"
    # Policy: a failing default-branch lookup degrades to None; a failing state lookup raises (status warns).
    fake.fail_always("Repository.default_branch", CodeHostError("boom"))
    assert await services.repo_info() is None
    fake.fail_always("ChangeRequests.state", CodeHostError("boom"))
    with pytest.raises(CodeHostError):
        await services.pr_state(cr.number)


def test_default_services_github_kind_needs_work_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _github_config(tmp_path)
    fake = InMemoryCodeHost(capabilities=frozenset(Capability) - {Capability.CHECKS, Capability.WORK_ITEMS})
    monkeypatch.setattr(cli, "create_host", lambda _config, _repo_dir: (fake.code_host(), None))
    with pytest.raises(ConfigurationError, match=r'kind = "github"'):
        cli.default_services(cfg)
    assert fake.calls == []  # stops before the first host call
    # With WorkItems the same config starts and uses the host tracker.
    ok = InMemoryCodeHost()
    monkeypatch.setattr(cli, "create_host", lambda _config, _repo_dir: (ok.code_host(), None))
    assert isinstance(cli.default_services(cfg).tracker, HostIssueTracker)


def test_services_turns_configuration_error_into_config_error_exit(tmp_path: Path) -> None:
    def factory(_config: SisyphusConfig) -> cli.Services:
        raise ConfigurationError('[source.work_item] kind = "github" needs WorkItems')

    with pytest.raises(typer.Exit) as exc:
        cli._services(factory, _github_config(tmp_path))
    assert exc.value.exit_code == 1


def test_default_services_file_kind_builds_file_tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    env = make_env(tmp_path)
    text = env.config.read_text().replace(
        'kind = "linear"\nassignee = "me@x.io"\npick_state = "Todo"', f'kind = "file"\ndir = "{tmp_path / "inbox"}"'
    )
    env.config.write_text(text)
    services = cli.default_services(load_config(env.config))
    assert isinstance(services.tracker, cli.FileTracker)


FORBIDDEN_IMPORTS = ("dotharness.pr_review", "dotharness.vibe_heal", "dotharness.spec_prism_flow")


def _forbidden_imports(source: str) -> list[str]:
    names: list[str] = []
    for n in ast.walk(ast.parse(source)):
        if isinstance(n, ast.Import):
            names += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
            names += [n.module, *(f"{n.module}.{a.name}" for a in n.names)]
    return [n for n in names if any(n == p or n.startswith(p + ".") for p in FORBIDDEN_IMPORTS)]


@pytest.mark.parametrize("path", sorted(Path(cli.__file__).parent.glob("*.py")), ids=lambda p: p.name)
def test_no_pr_review_vibe_heal_or_spec_prism_flow_import(path: Path) -> None:
    assert _forbidden_imports(path.read_text()) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "import dotharness.pr_review",
        "from dotharness.spec_prism_flow import config",
        "from dotharness import vibe_heal",
    ],
)
def test_import_guard_detects_forbidden_import(snippet: str) -> None:
    assert _forbidden_imports(snippet)


def test_default_path_without_linear_key_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    env = make_env(tmp_path)
    result = CliRunner().invoke(app, ["run", "--config", str(env.config)])
    assert result.exit_code == 1
    assert "LINEAR_API_KEY" in result.stderr


async def test_agent_runner_timeout(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    async def hung(*_a: Any, **kw: Any) -> RunResult:
        seen.update(kw)
        return _result(124, timed_out=True)

    with pytest.raises(AgentError, match="timed out after 2 minutes"):
        await make_agent_runner("claude", tmp_path, hung, timeout_minutes=2)("x")
    assert seen["timeout"] == 120


# --------------------------------------------------------------------- archive


def _archive_records(ws: Path) -> None:
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    ab = RunRecord.open(ws, "AB-1")
    ab.mark_stage("build")
    write_stop_record(
        ab.path,
        build_abandoned_record(
            "AB-1", stage="build", stage_status="in_progress", prior_label="l", reenable_command="r"
        ),
    )
    RunRecord.open(ws, "RUN-1").mark_stage("build", "in_progress")
    cp = RunRecord.open(ws, "CP-1")
    cp.mark_stage(OPEN_PR_STAGE, "complete")
    cp.set_checkpoint("C1")
    err = RunRecord.open(ws, "ERR-1")
    err.mark_stage(OPEN_PR_STAGE, "complete")
    write_stop_record(
        err.path,
        build_stop_record(
            "ERR-1", checkpoint=None, stage=OPEN_PR_STAGE, stage_status="complete", detail="x", resume_command="r"
        ),
    )
    gf = RunRecord.open(ws, "GF-1")
    gf.mark_stage(OPEN_PR_STAGE, "complete")
    gf.retries[GATE_FAILED_KEY] = 1
    gf.save()


def _ws_files(ws: Path) -> set[str]:
    return {p.name for p in ws.iterdir()}


def test_archive_moves_only_settled_items(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    _archive_records(ws)
    result = env.invoke("archive", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    arch = ws / "archive"
    assert sorted(result.stdout.splitlines()) == sorted([
        f"AB-1 -> {arch}",
        f"FIN-1 -> {arch}",
        f"GF-1 -> {arch}",
        "archived 3 item(s)",
    ])
    for ref in ("AB-1", "FIN-1", "GF-1"):
        assert not record_path(ws, ref).exists()
        assert (arch / record_path(ws, ref).name).exists()
    for ref in ("RUN-1", "CP-1", "ERR-1"):
        assert record_path(ws, ref).exists()
        assert not (arch / record_path(ws, ref).name).exists()


def test_archive_empty_run_is_quiet(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    RunRecord.open(tmp_path / "ws", "RUN-1").mark_stage("build", "in_progress")
    result = env.invoke("archive", "--config", str(env.config))
    assert (result.exit_code, result.output) == (0, "")
    assert not (tmp_path / "ws" / "archive").exists()


def test_archive_second_run_is_quiet(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    RunRecord.open(tmp_path / "ws", "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    assert env.invoke("archive", "--config", str(env.config)).exit_code == 0
    result = env.invoke("archive", "--config", str(env.config))
    assert (result.exit_code, result.output) == (0, "")


def test_archive_collision_refuses_item_others_continue(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    RunRecord.open(ws, "FIN-2").mark_stage(OPEN_PR_STAGE, "complete")
    name = record_path(ws, "FIN-1").name
    (ws / "archive").mkdir()
    (ws / "archive" / name).write_text("existing")
    result = env.invoke("archive", "--config", str(env.config))
    assert result.exit_code == 1
    assert f"FIN-1: not archived: {name} already exists in" in result.stderr
    assert result.stdout.splitlines() == [f"FIN-2 -> {ws / 'archive'}", "archived 1 item(s)"]
    assert record_path(ws, "FIN-1").exists()
    assert (ws / "archive" / name).read_text() == "existing"
    assert not record_path(ws, "FIN-2").exists()


def test_archive_oserror_per_item_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    RunRecord.open(ws, "FIN-2").mark_stage(OPEN_PR_STAGE, "complete")
    real = cli.archive_item

    def flaky(config: Any, rec: Any, archive_dir: Path) -> Any:
        if rec.item == "FIN-1":
            raise PermissionError("denied")
        return real(config, rec, archive_dir)

    monkeypatch.setattr(cli, "archive_item", flaky)
    result = env.invoke("archive", "--config", str(env.config))
    assert result.exit_code == 1
    assert "FIN-1: not archived: denied" in result.stderr
    assert "FIN-2 ->" in result.stdout
    assert record_path(ws, "FIN-1").exists()


def test_archive_lock_held_exits_zero_and_moves_nothing(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    with clone_lock(tmp_path / "repo", tmp_path / "locks"):
        result = env.invoke("archive", "--config", str(env.config))
    assert (result.exit_code, result.output) == (0, "")
    assert record_path(ws, "FIN-1").exists()


def test_archive_makes_no_pr_lookup(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    RunRecord.open(tmp_path / "ws", "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")

    async def boom(_n: int) -> str:
        raise AssertionError("gh called")

    env.services.pr_state = boom  # type: ignore[assignment]
    assert env.invoke("archive", "--config", str(env.config)).exit_code == 0


@pytest.mark.parametrize("archive_dir", ["{ws}", "{root}", "."])
def test_archive_dir_equal_or_containing_workspace_fails(tmp_path: Path, archive_dir: str) -> None:
    ws = tmp_path / "ws"
    value = archive_dir.format(ws=ws, root=tmp_path)
    env = make_env(tmp_path, extra=f'\n[archive]\ndir = "{value}"\n')
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    result = env.invoke("archive", "--config", str(env.config))
    assert result.exit_code == 1
    assert "config error" in result.stderr
    assert record_path(ws, "FIN-1").exists()


def test_archive_custom_absolute_dir(tmp_path: Path) -> None:
    dest = tmp_path / "elsewhere"
    env = make_env(tmp_path, extra=f'\n[archive]\ndir = "{dest}"\n')
    ws = tmp_path / "ws"
    RunRecord.open(ws, "FIN-1").mark_stage(OPEN_PR_STAGE, "complete")
    result = env.invoke("archive", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    assert (dest / record_path(ws, "FIN-1").name).exists()


def test_archive_requires_workspace_dir(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.config.write_text(env.config.read_text().replace(f'workspace_dir = "{tmp_path / "ws"}"\n', ""))
    result = env.invoke("archive", "--config", str(env.config))
    assert result.exit_code == 1
    assert "workspace_dir is required" in result.stderr


def test_run_passes_resolved_archive_dir_to_pick(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    seen: dict[str, Any] = {}
    real_pick = cli.pick_items

    def pick(*a: Any, **k: Any) -> Any:
        seen.update(k)
        return real_pick(*a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "pick_items", pick)
        result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0, result.output
    assert seen["archive_dir"] == tmp_path / "ws" / "archive"


def test_run_does_not_repick_archived_item(tmp_path: Path, fake_run_item: dict[str, Any]) -> None:
    env = make_env(tmp_path, ["ENG-1"])
    ws = tmp_path / "ws"
    RunRecord.open(ws, "ENG-1").mark_stage(OPEN_PR_STAGE, "complete")
    assert env.invoke("archive", "--config", str(env.config)).exit_code == 0
    result = env.invoke("run", "--config", str(env.config), "--base", "b")
    assert result.exit_code == 0, result.output
    assert fake_run_item["calls"] == []


def test_no_args_prints_help() -> None:
    result = CliRunner().invoke(app, [])
    assert "Usage" in result.output
    assert "status" in result.output


def test_missing_config_option_exits_2() -> None:
    assert CliRunner().invoke(app, ["run"]).exit_code == 2
    assert CliRunner().invoke(app, ["status"]).exit_code == 2


def test_config_directory_rejected_exits_2(tmp_path: Path) -> None:
    assert CliRunner().invoke(app, ["run", "--config", str(tmp_path)]).exit_code == 2


def test_dotharness_cli_exposes_sisyphus() -> None:
    from dotharness import cli as top

    assert top.sisyphus is app


def test_status_unknown_ref_exact_stderr(tmp_path: Path) -> None:
    env = _status_workspace(tmp_path)
    result = env.invoke("status", "NOPE-1", "--config", str(env.config))
    assert result.exit_code == 1
    assert result.stderr == "Error: no run record for NOPE-1\n"
    assert result.stdout == ""


def test_missing_config_exact_stderr_prefix(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--config", str(tmp_path / "missing.toml")])
    assert result.exit_code == 1
    assert result.stderr.startswith("Error: config error: ")
    assert result.stderr.count("Error:") == 1


@pytest.mark.parametrize(
    ("flag", "config_base", "default", "base", "intake"),
    [
        (None, None, "trunk", "main", "trunk"),  # no base given: PR base unchanged, intake uses the default branch
        (None, None, None, "main", "main"),
        ("rel", "cfg", "trunk", "rel", "rel"),
        (None, "cfg", "trunk", "cfg", "cfg"),
    ],
)
async def test_resolve_options_intake_base(
    tmp_path: Path, flag: str | None, config_base: str | None, default: str | None, base: str, intake: str
) -> None:
    async def repo_info() -> str | None:
        return default

    env = make_env(tmp_path, [])
    cfg = load_config(env.config)
    cfg = replace(cfg, project=replace(cfg.project, base_branch=config_base))
    options = await cli._resolve_options(cfg, replace(env.services, repo_info=repo_info), flag)
    assert (options.base_branch, options.intake_base) == (base, intake)
