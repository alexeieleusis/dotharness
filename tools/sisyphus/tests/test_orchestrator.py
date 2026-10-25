"""Orchestrator tests: every external effect (agent, subprocess, PR client) is a fake.

Only the intake stage's own ``git status`` snapshot touches a real (temporary) git repository.
No test runs an agent, harness, spec-prism-flow, gh or ``git push``.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sisyphus import orchestrator, pick
from sisyphus.cleanup import PrInfo
from sisyphus.config import DigestConfig, LimitsConfig, MergeConfig, ProjectConfig, SisyphusConfig
from sisyphus.digest import DigestWriteError
from sisyphus.gaterecord import gate_record_path, read_gate_record
from sisyphus.intake import resolve_brief_path
from sisyphus.lock import LockHeld, clone_lock
from sisyphus.orchestrator import (
    CommandResult,
    GitHubError,
    OrchestratorError,
    OrchestratorOptions,
    PullRequest,
    branch_name,
    build_orchestration_prompt,
    default_subprocess_runner,
    dump_spf_config,
    package_dirs,
    plan_dir_name,
    read_build_commands,
    run_item,
    stop_report_path,
)
from sisyphus.paths import find_checkout_root, knowledge_file, tool_dir
from sisyphus.rails import Checkpoint
from sisyphus.runrecord import RunRecord
from sisyphus.stoprecord import build_abandoned_record, write_stop_record

ITEM = "ENG-1"
SHA1 = "1" * 40
SHA2 = "2" * 40
SHA3 = "3" * 40
SHA4 = "4" * 40
BRIEF_NONE = "# Brief\n\n## Open decisions log\n\nNone\n"
BRIEF_OPEN = "# Brief\n\n## Open decisions log\n\n1. Which tracker?\n"
TODAY = datetime(2026, 1, 2, tzinfo=UTC)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)  # noqa: S603, S607


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class World:
    """Shared event log plus the three fakes, wired to one temporary world."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "tools" / "pkg").mkdir(parents=True)
        (self.repo / "tools" / "pkg" / "Makefile").write_text("check:\n")
        (self.repo / "tracked.txt").write_text("one\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-q", "-m", "init")
        self.phases = tmp_path / "phases"
        self.phases.mkdir()
        self.state = tmp_path / "state"
        self.clock = Clock()
        self.events: list[str] = []
        self.shell_calls: list[tuple[tuple[str, ...], Path | None]] = []
        # agent behaviour
        self.brief_text = BRIEF_NONE
        self.leaves = 2
        self.plan_stop: dict | None = None
        self.build_stops: list[dict | None] = []
        self.agent_prompts: list[str] = []
        self.touch_repo_in_intake = False
        self.requirements: str | None = None  # written to the plan dir (with a matching manifest) when set
        self.git_log = ""
        self.git_log_rc = 0
        self.log_calls: list[list[str]] = []
        # git behaviour
        self.head = SHA1
        self.remote_head = SHA1
        self.remote_exists = False
        self.branch_exists = False
        self.ancestor_rc = 0
        self.ancestor_rc_after_pr = 0
        self.push_rc = 0
        self.fetch_rc = 0
        self.ff_rc = 0
        self.ahead = "1"
        self.current_branch: str | None = None
        self.changed = "tools/pkg/x.py\n"
        self.gate_rc = 0
        self.dirty = ""
        self.checkouts: list[str] = []
        self.branch_present_after_pr = False  # the local branch is still there when the cleanup probes for it
        self.branch_cmds: list[list[str]] = []
        self.ci_fix_pushes: list[str] = []  # new head per ci-fix call; exhausted -> no push
        self.ci_fix_rc = 0
        self.merge_rc = 0
        # PR client behaviour
        self.existing_pr: PullRequest | None = None
        self.opened: list[tuple[str, str, str, str]] = []
        self.client_error: GitHubError | None = None
        self.pr_info: PrInfo | None = None
        self.pr_state_error: GitHubError | None = None
        self.pr_state_refs: list[str] = []
        self.find_calls = 0
        # config
        self.limits = LimitsConfig()
        self.merge_enabled = False
        (self.repo / ".spec-prism-flow.toml").write_text('[build]\ncommands = ["make check", "make test"]\n')

    # ---- config / options
    def config(self) -> SisyphusConfig:
        return SisyphusConfig(
            project=ProjectConfig(
                repo_dir=str(self.repo),
                workspace_dir=str(self.tmp / "ws"),
                brief_path_template="<workspace_dir>/<item-id>/brief.md",
            ),
            limits=self.limits,
            merge=MergeConfig(enabled=self.merge_enabled),
            digest=DigestConfig(path_template=str(self.tmp / "digest-<date>.md")),
        )

    def options(self, **kw: object) -> OrchestratorOptions:
        base = OrchestratorOptions(
            base_branch="base-branch",
            repo_slug="owner-repo",
            phase_dir=self.phases,
            state_dir=self.state,
            lock_dir=self.tmp / "locks",
            home=self.tmp / "nohome",
        )
        return replace(base, **kw)

    # ---- fakes
    def agent(self, prompt: str) -> None:
        self.agent_prompts.append(prompt)
        match = re.search(r"Save the brief file to (\S+)\.", prompt)
        if match:
            self.events.append("agent:intake")
            brief = Path(match.group(1))
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.write_text(self.brief_text)
            if self.touch_repo_in_intake:
                (self.repo / "tracked.txt").write_text("changed\n")
            return
        stage = re.search(r"Stage to run now: `(\w+)`", prompt).group(1)  # ty: ignore[unresolved-attribute]
        self.events.append(f"agent:{stage}")
        stop_path = Path(re.search(r"Stop report file: (\S+)", prompt).group(1))  # ty: ignore[unresolved-attribute]
        if stage == "plan":
            self.clock.now += 0
            for i in range(self.leaves):
                (self.phases / f"{i:02d}-leaf{i}-leaf.md").write_text("x")
            if self.requirements is not None:
                plan_dir = self.tmp / "ws" / orchestrator.plan_dir_name(ITEM)
                plan_dir.mkdir(parents=True, exist_ok=True)
                (plan_dir / "requirements.md").write_text(self.requirements)
                manifest = {"brief": str(resolve_brief_path(self.config(), ITEM))}
                (plan_dir / "init_manifest.json").write_text(json.dumps(manifest))
            if self.plan_stop:
                stop_path.write_text(json.dumps(self.plan_stop))
        else:
            stop = self.build_stops.pop(0) if self.build_stops else None
            if stop:
                stop_path.write_text(json.dumps(stop))

    def shell(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        a = list(argv)
        self.shell_calls.append((tuple(a), cwd))
        if a[0] == "git":
            return self._git(a[1:])
        if a[:3] == ["harness", "run", "ci-fix"]:
            self.events.append("ci-fix")
            if self.ci_fix_pushes:
                self.remote_head = self.ci_fix_pushes.pop(0)
            return CommandResult(self.ci_fix_rc)
        if a[:3] == ["harness", "run", "merge"]:
            self.events.append("merge")
            return CommandResult(self.merge_rc)
        if a[0] == "make":
            self.events.append(f"gate:{' '.join(a)}@{cwd.name if cwd else None}")
            return CommandResult(self.gate_rc, "", "gate failed" if self.gate_rc else "")
        raise AssertionError(f"unexpected command {a}")

    def _git(self, a: list[str]) -> CommandResult:  # noqa: C901
        sub = a[0]
        if sub == "fetch":
            return CommandResult(self.fetch_rc, "", "network down" if self.fetch_rc else "")
        if sub == "rev-parse" and a[1] == "--verify":
            present = self.branch_exists or (self.branch_present_after_pr and self.existing_pr is not None)
            return CommandResult(0 if present else 1)
        if sub == "rev-parse" and a[1].startswith("refs/heads/"):
            return CommandResult(0, f"{SHA1}\n")
        if sub == "branch":
            self.branch_cmds.append(a)
            if a[1] == "-d":
                self.branch_present_after_pr = False
            return CommandResult(0)
        if sub == "status":
            return CommandResult(0, self.dirty)
        if sub == "log":
            self.log_calls.append(a)
            return CommandResult(self.git_log_rc, self.git_log, "boom" if self.git_log_rc else "")
        if sub == "checkout":
            self.checkouts.append(a[-1])
            if a[1] == "-b":
                self.current_branch = a[2]
            else:
                self.current_branch = a[1]
            return CommandResult(0)
        if sub == "rev-parse" and a[1:] == ["--abbrev-ref", "HEAD"]:
            return CommandResult(0, f"{self.current_branch}\n")
        if sub == "rev-list":
            return CommandResult(0, f"{self.ahead}\n")
        if sub == "ls-remote":
            return CommandResult(0, f"{SHA1}\trefs/heads/{a[-1]}\n" if self.remote_exists else "")
        if sub == "merge-base":
            return CommandResult(self.ancestor_rc if self.existing_pr is None else self.ancestor_rc_after_pr)
        if sub == "push":
            self.events.append(f"git {' '.join(a)}")
            return CommandResult(self.push_rc, "", "denied" if self.push_rc else "")
        if sub == "rev-parse" and a[1] == "HEAD":
            return CommandResult(0, f"{self.head}\n")
        if sub == "rev-parse" and a[1] == "FETCH_HEAD":
            return CommandResult(0, f"{self.remote_head}\n")
        if sub == "diff":
            return CommandResult(0, self.changed)
        if sub == "merge":
            if self.ff_rc == 0:
                self.head = self.remote_head
            return CommandResult(self.ff_rc)
        raise AssertionError(f"unexpected git {a}")

    def find_pr(self, branch: str) -> PullRequest | None:
        self.find_calls += 1
        self.events.append("find_pr")
        return self.existing_pr

    def pr_state(self, ref: str) -> PrInfo | None:
        self.pr_state_refs.append(ref)
        if self.pr_state_error:
            raise self.pr_state_error
        return self.pr_info

    def open_pr(self, branch: str, base: str, title: str, body: str) -> PullRequest:
        self.events.append("open_pr")
        if self.client_error:
            raise self.client_error
        self.opened.append((branch, base, title, body))
        self.existing_pr = PullRequest(42, "https://example.test/pr/42")
        return self.existing_pr

    # ---- run
    def run(self, lock_held: bool = False, **kw: object) -> orchestrator.RunResult:
        return run_item(
            ITEM,
            self.config(),
            self.options(**kw),
            lock_held=lock_held,
            agent_runner=self.agent,
            subprocess_runner=self.shell,
            pr_client=self,
            clock=self.clock,
            today=TODAY,
        )

    def digest_text(self) -> str:
        # a digest is never overwritten: later runs of the same day land in -2, -3...; read the latest
        first = self.tmp / "digest-2026-01-02.md"
        later = sorted(self.tmp.glob("digest-2026-01-02-*.md"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        return (later[-1] if later else first).read_text()

    def counts(self) -> tuple[int, int, int]:
        return len(self.agent_prompts), len(self.shell_calls), self.find_calls

    def mark_log(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
        log: list[tuple[str, str]] = []
        original = RunRecord.mark_stage

        def spy(rec: RunRecord, stage: str, status: str = "in_progress") -> None:
            log.append((stage, status))
            original(rec, stage, status)

        monkeypatch.setattr(RunRecord, "mark_stage", spy)
        return log


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _needs_you_lines(digest: str, cp: str) -> list[str]:
    section = digest.split("## Needs you")[1].split("## Done")[0]
    return [line for line in section.splitlines() if f"[{cp}]" in line]


def _assert_stop(world: World, result: orchestrator.RunResult, cp: Checkpoint) -> None:
    assert result.checkpoint is cp
    assert not result.completed
    assert RunRecord.open(world.tmp / "ws", ITEM).checkpoint == cp.value
    assert len(_needs_you_lines(world.digest_text(), cp.value)) == 1


# ------------------------------------------------------------------ AC1, happy path


def test_stage_order_and_record_writes(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    log = world.mark_log(monkeypatch)
    result = world.run()
    assert result.completed
    assert result.stages_run == ("intake", "plan", "build", "push", "open-pr")
    expected = []
    for stage in result.stages_run:
        expected += [(stage, "in_progress"), (stage, "complete")]
    assert log == expected  # a record write before and after each stage, in order
    kinds = [e for e in world.events if e.startswith(("agent", "git push", "open_pr", "gate"))]
    assert kinds == [
        "agent:intake",
        "agent:plan",
        "agent:build",
        f"git push origin {branch_name(ITEM)}",
        "open_pr",
        "gate:make check@pkg",
        "gate:make test@pkg",
    ]


def test_happy_path_pr_gate_and_digest(world: World) -> None:
    result = world.run()
    assert world.opened == [(branch_name(ITEM), "base-branch", "Sisyphus: ENG-1", world.opened[0][3])]
    assert result.pr == PullRequest(42, "https://example.test/pr/42")
    assert result.gate_recorded
    assert read_gate_record("owner-repo", 42, world.state) == SHA1
    data = json.loads(gate_record_path("owner-repo", 42, world.state).read_text())
    assert data["commands"] == ["make check", "make test"]
    digest = world.digest_text()
    assert "PR #42 open" in digest
    assert f"gate record written for {SHA1}" in digest
    assert any(c[0] == ("git", "checkout", "-b", branch_name(ITEM), "origin/base-branch") for c in world.shell_calls)
    assert RunRecord.open(world.tmp / "ws", ITEM).retries.get("branch-created") == 1


def test_base_branch_is_a_parameter_not_a_literal() -> None:
    src = Path(orchestrator.__file__).read_text()
    assert "95_pr-review" not in src
    assert '"main"' not in src


def test_prompts_carry_resolved_paths(world: World) -> None:
    world.run()
    plan, build = world.agent_prompts[1], world.agent_prompts[2]
    root = find_checkout_root()
    assert root is not None
    cheat = str(tool_dir(world.tmp / "nohome", root) / "docs" / "lifecycle-cheat-sheet.md")
    for prompt, stage in ((plan, "plan"), (build, "build")):
        assert f"Stage to run now: `{stage}`" in prompt
        assert cheat in prompt
        assert str(knowledge_file("spec-prism-flow/plan-generation-prompt.md", world.tmp / "nohome", root)) in prompt
        assert str(knowledge_file("spec-prism-flow/build-execution-prompt.md", world.tmp / "nohome", root)) in prompt
        assert f"Target branch (already checked out in that checkout): {branch_name(ITEM)}" in prompt
        assert not re.search(
            r"<(stage|brief path|workspace dir|target repo dir|target branch|stop report path)>", prompt
        )


# ---------------------------------------------------------------- AC2: checkpoints


def test_c1_brief_open_decisions(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    result = world.run()
    _assert_stop(world, result, Checkpoint.C1)
    assert result.stages_run == ()  # intake did not finish its stage in this run
    assert world.events == ["agent:intake"]  # no plan agent, no git


def test_c2_open_questions_after_overview(world: World) -> None:
    world.plan_stop = {"checkpoint": "C2", "leaf": "", "detail": "Which API version?"}
    result = world.run()
    _assert_stop(world, result, Checkpoint.C2)
    assert "agent:build" not in world.events
    assert "Which API version?" in world.digest_text()
    assert not any(c[0][:2] == ("git", "fetch") for c in world.shell_calls)


def test_c3_leaf_open_question(world: World) -> None:
    world.build_stops = [{"checkpoint": "C3", "leaf": "13-foo", "detail": "Use A or B?"}]
    result = world.run()
    _assert_stop(world, result, Checkpoint.C3)
    assert "Use A or B?" in result.detail
    assert not any(c[0][:2] == ("git", "push") for c in world.shell_calls)
    assert world.find_calls == 0


def test_c4_deviation_changes_later_leaf(world: World) -> None:
    world.build_stops = [{"checkpoint": "C4", "leaf": "13-foo", "detail": "cannot tell"}]
    result = world.run()
    _assert_stop(world, result, Checkpoint.C4)
    assert not any(c[0][:2] == ("git", "push") for c in world.shell_calls)


def test_c5_leaf_scope_violation_from_agent(world: World) -> None:
    world.build_stops = [{"checkpoint": "C5", "leaf": "13-foo", "detail": "touched config.py"}]
    result = world.run()
    _assert_stop(world, result, Checkpoint.C5)
    assert RunRecord.open(world.tmp / "ws", ITEM).retries["build:retry-cycles"] == 1
    assert world.find_calls == 0


def test_c5_gate_command_failure_writes_no_record(world: World) -> None:
    world.gate_rc = 2
    result = world.run()
    _assert_stop(world, result, Checkpoint.C5)
    assert result.pr is not None  # PR is open, but not gated
    assert not result.gate_recorded
    assert read_gate_record("owner-repo", 42, world.state) is None
    assert not gate_record_path("owner-repo", 42, world.state).exists()
    assert world.events.count("gate:make check@pkg") == 1  # stopped at first failure


def test_c5_intake_boundary_violation(world: World) -> None:
    world.touch_repo_in_intake = True
    result = world.run()
    _assert_stop(world, result, Checkpoint.C5)
    assert world.events == ["agent:intake"]


def test_c6_five_leaves(world: World) -> None:
    world.leaves = 5
    result = world.run()
    _assert_stop(world, result, Checkpoint.C6)
    assert "5 leaves" in result.detail
    assert "agent:build" not in world.events
    assert "Limits hit" in world.digest_text()
    assert "max_leaves_per_run" in world.digest_text().split("## Limits hit")[1]


def test_four_leaves_is_allowed(world: World) -> None:
    world.leaves = 4
    assert world.run().completed


def test_c6_leaf_limit_rerun_does_not_replan(world: World) -> None:
    world.leaves = 5
    world.run()
    plans_before = world.events.count("agent:plan")
    result = world.run()
    assert result.checkpoint is Checkpoint.C6
    assert world.events.count("agent:plan") == plans_before
    world.limits = LimitsConfig(max_leaves_per_run=5)  # the human raised the limit
    result = world.run()
    assert result.completed
    assert world.events.count("agent:plan") == plans_before


def test_c6_run_time_limit(world: World) -> None:
    original = world.agent

    def slow(prompt: str) -> None:
        original(prompt)
        world.clock.now += 3 * 3600

    world.agent_prompts = []
    result = run_item(
        ITEM,
        world.config(),
        world.options(),
        agent_runner=slow,
        subprocess_runner=world.shell,
        pr_client=world,
        clock=world.clock,
        today=TODAY,
    )
    _assert_stop(world, result, Checkpoint.C6)
    assert "agent:plan" not in world.events


def test_c7_branch_needs_rebase(world: World) -> None:
    world.remote_exists = True
    world.ancestor_rc = 1
    result = world.run()
    _assert_stop(world, result, Checkpoint.C7)
    assert not any(c[0][:2] == ("git", "push") for c in world.shell_calls)
    assert world.find_calls == 0


def test_c8_pr_client_failure(world: World) -> None:
    world.client_error = GitHubError("auth failed")
    result = world.run()
    _assert_stop(world, result, Checkpoint.C8)
    assert "auth failed" in result.detail
    assert not any(e.startswith("gate") for e in world.events)


def test_c8_push_failure(world: World) -> None:
    world.push_rc = 1
    result = world.run()
    _assert_stop(world, result, Checkpoint.C8)
    assert world.find_calls == 0


def test_c8_network_fetch_failure(world: World) -> None:
    world.fetch_rc = 1
    result = world.run()
    _assert_stop(world, result, Checkpoint.C8)
    assert "agent:build" not in world.events


# ------------------------------------------------------------ resume and retries


def test_resume_after_c3_relaunches_build_without_replanning(world: World) -> None:
    world.build_stops = [{"checkpoint": "C3", "leaf": "13-foo", "detail": "Use A or B?"}]
    world.run()
    world.branch_exists = True
    result = world.run()
    assert result.completed
    assert result.stages_run == ("build", "push", "open-pr")
    assert result.stages_skipped == ("intake", "plan")
    assert world.events.count("agent:plan") == 1
    assert world.events.count("agent:build") == 2
    assert RunRecord.open(world.tmp / "ws", ITEM).checkpoint is None


def test_build_not_relaunched_after_retry_cycles_exhausted(world: World) -> None:
    world.build_stops = [{"checkpoint": "C5", "detail": "again"}] * 3
    for _ in range(3):
        assert world.run().checkpoint is Checkpoint.C5
        world.branch_exists = True
    builds = world.events.count("agent:build")
    result = world.run()
    assert result.checkpoint is Checkpoint.C5
    assert world.events.count("agent:build") == builds == 3


def test_bad_stop_reports_raise(world: World) -> None:
    world.plan_stop = {"checkpoint": "C3", "detail": "wrong stage"}
    with pytest.raises(OrchestratorError, match="not valid for stage plan"):
        world.run()
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    assert (rec.stage, rec.stage_status) == ("plan", "in_progress")
    assert "# Sisyphus digest" in world.digest_text()  # digest still written when a stage raises


def test_build_without_commits_raises(world: World) -> None:
    world.ahead = "0"
    with pytest.raises(OrchestratorError, match="no commits"):
        world.run()


def test_preexisting_branch_not_created_by_sisyphus_is_refused(world: World) -> None:
    world.branch_exists = True
    with pytest.raises(OrchestratorError, match="did not create it"):
        world.run()
    assert "agent:build" not in world.events


# ------------------------------------------------------------------- AC3: idempotence


def test_rerun_with_unchanged_inputs_repeats_nothing(world: World) -> None:
    world.run()
    before = world.counts()
    events = list(world.events)
    result = world.run()
    assert result.completed
    assert result.stages_run == ()
    assert result.stages_skipped == ("intake", "plan", "build", "push", "open-pr")
    agents, _shells, finds = world.counts()
    assert (agents, finds) == (before[0], before[2])
    # the only extra call is the read-only "is the local branch still there?" probe of the branch cleanup
    assert [c[0] for c in world.shell_calls[before[1] :]] == [
        ("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name(ITEM)}")
    ]
    assert world.events == events
    assert len(world.opened) == 1


def test_rerun_after_gate_failure_reuses_existing_pr(world: World) -> None:
    world.gate_rc = 1
    world.run()
    world.gate_rc = 0
    result = world.run()
    assert result.completed
    assert len(world.opened) == 1  # find_pr found the PR; no second PR
    assert read_gate_record("owner-repo", 42, world.state) == SHA1
    assert world.events.count("agent:build") == 1  # earlier stages not repeated
    assert world.events.count("agent:plan") == 1


def test_rerun_after_c1_unchanged_brief_makes_no_agent_call(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    world.run()
    result = world.run()
    assert result.checkpoint is Checkpoint.C1
    assert world.events == ["agent:intake"]  # the sidecar skip means no second intake call


# --------------------------------------------------------------- AC5: push guard


def test_push_to_protected_branch_is_refused(world: World) -> None:
    protected = branch_name(ITEM)
    with pytest.raises(OrchestratorError, match="push refused: push to the default branch"):
        world.run(default_branch=protected)
    assert not any(c[0][:2] == ("git", "push") for c in world.shell_calls)
    assert world.find_calls == 0


def test_push_refused_when_branch_not_recorded_as_created(world: World) -> None:
    world.run()
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    rec.retries.pop("branch-created")
    rec.mark_stage("build", "complete")
    world.shell_calls.clear()
    with pytest.raises(OrchestratorError, match="not created by Sisyphus"):
        world.run()
    assert not any(c[0][:2] == ("git", "push") for c in world.shell_calls)


def test_push_argv_is_plain_non_force(world: World) -> None:
    world.run()
    pushes = [c[0] for c in world.shell_calls if c[0][:2] == ("git", "push")]
    assert pushes == [("git", "push", "origin", branch_name(ITEM))]


# ------------------------------------------------------------- AC6: ci-fix and merge


def test_ci_fix_argv_and_attempt_cap(world: World) -> None:
    world.limits = LimitsConfig(max_fix_attempts_per_pr=2)
    world.ci_fix_pushes = [SHA2, SHA3, SHA4]
    result = world.run(ci_fix=True)
    argvs = [c[0] for c in world.shell_calls if c[0][0] == "harness"]
    assert argvs == [("harness", "run", "ci-fix", "--pr", "42")] * 2
    assert list(argvs[0]) == ["harness", "run", "ci-fix", "--pr", "42"]
    _assert_stop(world, result, Checkpoint.C6)
    assert RunRecord.open(world.tmp / "ws", ITEM).retries["ci-fix:attempts"] == 2
    # every ci-fix push re-gated the PR at the new head
    assert read_gate_record("owner-repo", 42, world.state) == SHA3
    assert world.events.count("gate:make check@pkg") == 3


def test_ci_fix_cap_not_exceeded_on_rerun(world: World) -> None:
    world.limits = LimitsConfig(max_fix_attempts_per_pr=1)
    world.ci_fix_pushes = [SHA2, SHA3]
    world.run(ci_fix=True)
    result = world.run(ci_fix=True)
    assert result.checkpoint is Checkpoint.C6
    assert sum(1 for c in world.shell_calls if c[0][:3] == ("harness", "run", "ci-fix")) == 1


def test_ci_fix_with_nothing_to_fix_settles_after_one_call(world: World) -> None:
    result = world.run(ci_fix=True)
    assert result.completed
    assert sum(1 for c in world.shell_calls if c[0][:3] == ("harness", "run", "ci-fix")) == 1
    assert read_gate_record("owner-repo", 42, world.state) == SHA1
    assert world.events.count("gate:make check@pkg") == 1  # no new head, no re-gate


def test_ci_fix_that_pushes_once_then_settles_regates(world: World) -> None:
    world.ci_fix_pushes = [SHA2]
    result = world.run(ci_fix=True)
    assert result.completed
    assert read_gate_record("owner-repo", 42, world.state) == SHA2
    assert result.stages_run[-1] == "ci-fix"


def test_ci_fix_failed_gate_after_fix_writes_no_new_record(world: World) -> None:
    world.ci_fix_pushes = [SHA2]
    original_shell = world.shell
    state = {"made": 0}

    def shell(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        a = list(argv)
        if a[0] == "make":
            state["made"] += 1
            if state["made"] > 2:
                world.gate_rc = 1
        return original_shell(argv, cwd)

    result = run_item(
        ITEM,
        world.config(),
        world.options(ci_fix=True),
        agent_runner=world.agent,
        subprocess_runner=shell,
        pr_client=world,
        clock=world.clock,
        today=TODAY,
    )
    _assert_stop(world, result, Checkpoint.C5)
    assert read_gate_record("owner-repo", 42, world.state) == SHA1  # stale for SHA2: the merge runner refuses


def test_ci_fix_ff_failure_is_c7(world: World) -> None:
    world.ci_fix_pushes = [SHA2]
    world.ff_rc = 1
    result = world.run(ci_fix=True)
    _assert_stop(world, result, Checkpoint.C7)


def test_ci_fix_command_failure_is_c8(world: World) -> None:
    world.ci_fix_rc = 1
    result = world.run(ci_fix=True)
    _assert_stop(world, result, Checkpoint.C8)


def test_ci_fix_disabled_by_default(world: World) -> None:
    world.run()
    assert not any(c[0][0] == "harness" for c in world.shell_calls)


def test_merge_runner_invoked_only_via_harness_cli_when_enabled(world: World) -> None:
    world.merge_enabled = True
    world.pr_info = PrInfo(42, "MERGED", SHA1)
    result = world.run()
    assert result.completed
    merges = [c[0] for c in world.shell_calls if c[0][:3] == ("harness", "run", "merge")]
    assert merges == [("harness", "run", "merge", "--pr", "42")]
    assert result.stages_run[-1] == "merge"


def test_merge_not_run_when_disabled(world: World) -> None:
    world.run()
    assert not any("merge" in c[0] and c[0][0] == "harness" for c in world.shell_calls)


def test_nothing_ever_approves(world: World) -> None:
    world.merge_enabled = True
    world.pr_info = PrInfo(42, "MERGED", SHA1)
    world.run(ci_fix=True)
    for argv, _ in world.shell_calls:
        assert "approve" not in " ".join(argv)


def test_merge_command_failure_is_c8(world: World) -> None:
    world.merge_enabled = True
    world.merge_rc = 1
    _assert_stop(world, world.run(), Checkpoint.C8)


def test_merge_skipped_with_exit_zero_is_not_a_completed_merge(world: World) -> None:
    world.merge_enabled = True
    world.pr_info = PrInfo(42, "OPEN", SHA1)  # the runner exited 0 but skipped the merge
    result = world.run()
    _assert_stop(world, result, Checkpoint.C8)
    assert "not merged" in result.detail
    assert "merge" not in result.stages_run
    assert not world.branch_cmds  # the branch is not cleaned up


# ---------------------------------------------------------- gate commands (FR-3.3.10)


def test_no_build_commands_writes_no_record_and_reports_in_digest(world: World) -> None:
    (world.repo / ".spec-prism-flow.toml").write_text('[plan]\nphase_dir = "x"\n')
    result = world.run()
    assert result.completed
    assert not result.gate_recorded
    assert not gate_record_path("owner-repo", 42, world.state).exists()
    assert "no build.commands" in world.digest_text().split("## Deviations")[1]
    assert not any(e.startswith("gate") for e in world.events)


def test_item_config_points_at_this_items_plan_dir_not_the_repos(world: World) -> None:
    stale = world.tmp / "ws" / "plan-82"
    (world.repo / ".spec-prism-flow.toml").write_text(
        f'[plan]\nworkspace_dir = "{stale}"\nphase_dir = "{stale}/phases"\n\n'
        '[build]\ncommands = ["make check", "make test"]\nworkers = 1\n'
    )
    base_before = (world.repo / ".spec-prism-flow.toml").read_text()
    plan_dir = world.tmp / "ws" / plan_dir_name(ITEM)
    world.phases = plan_dir / "phases"
    world.phases.mkdir(parents=True)
    result = world.run(phase_dir=None)
    assert result.completed
    data = tomllib.loads((plan_dir / ".spec-prism-flow.toml").read_text())
    assert data["plan"] == {"workspace_dir": str(plan_dir), "phase_dir": str(plan_dir / "phases")}
    assert data["build"] == {"commands": ["make check", "make test"], "workers": 1}
    assert (world.repo / ".spec-prism-flow.toml").read_text() == base_before
    assert any(str(plan_dir / ".spec-prism-flow.toml") in p for p in world.agent_prompts)


def test_plan_dir_name() -> None:
    assert plan_dir_name("#118") == "plan-118"
    assert plan_dir_name("118") == "plan-118"
    assert plan_dir_name("owner/repo#7").startswith("plan-owner-repo-7-")


def test_plan_dir_name_distinguishes_refs_sharing_a_long_prefix() -> None:
    prefix = "https://linear.app/acme/issue/ENG-"
    a, b = plan_dir_name(f"{prefix}1/very-long-title"), plan_dir_name(f"{prefix}2/very-long-title")
    assert a != b
    assert plan_dir_name("owner/repo#7") != plan_dir_name("other/repo#7")


def test_dump_spf_config_round_trips() -> None:
    data = {"agent": {"backend": "claude"}, "build": {"commands": ["a b", 'q"x'], "workers": 2, "on": True}}
    assert tomllib.loads(dump_spf_config(data)) == data


def test_dump_spf_config_round_trips_nested_tables_quoted_keys_and_datetimes() -> None:
    src = (
        'when = 2024-01-02T03:04:05Z\n[build]\ncommands = ["make"]\n[build.env]\nFOO = "1"\n"my key" = "x"\n"a.b" = 1\n'
        "[[steps]]\nn = 1\n[[steps]]\nn = 2\n"
    )
    data = tomllib.loads(src)
    assert tomllib.loads(dump_spf_config(data)) == data


def test_missing_spf_config_writes_no_record(world: World) -> None:
    (world.repo / ".spec-prism-flow.toml").unlink()
    result = world.run()
    assert result.completed
    assert not gate_record_path("owner-repo", 42, world.state).exists()
    assert "no build.commands" in world.digest_text()


def test_gate_runs_in_each_touched_package_dir(world: World) -> None:
    (world.repo / "other").mkdir()
    (world.repo / "other" / "Makefile").write_text("x:\n")
    world.changed = "tools/pkg/a.py\nother/b.py\n"
    world.run()
    gates = [c for c in world.shell_calls if c[0][0] == "make"]
    assert {(c[0], c[1].name) for c in gates if c[1]} == {
        (("make", "check"), "pkg"),
        (("make", "test"), "pkg"),
        (("make", "check"), "other"),
        (("make", "test"), "other"),
    }


def test_gate_record_records_exact_tested_head(world: World) -> None:
    world.head = SHA4
    world.run()
    assert read_gate_record("owner-repo", 42, world.state) == SHA4


def test_package_dirs(tmp_path: Path) -> None:
    (tmp_path / "a" / "deep").mkdir(parents=True)
    (tmp_path / "a" / "Makefile").write_text("")
    (tmp_path / "b").mkdir()
    assert package_dirs(tmp_path, ["a/deep/x.py", "a/y.py", "b/z.txt", "top.md"]) == [tmp_path, tmp_path / "a"]


def test_read_build_commands(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    assert read_build_commands(p) is None
    p.write_text('[build]\ncommands = ["make check"]\n')
    assert read_build_commands(p) == ["make check"]
    p.write_text("[build]\ncommands = []\n")
    assert read_build_commands(p) is None
    p.write_text("[build]\ncommands = [1]\n")
    with pytest.raises(OrchestratorError, match="list of non-empty strings"):
        read_build_commands(p)
    p.write_text("= bad")
    with pytest.raises(OrchestratorError, match="cannot read"):
        read_build_commands(p)


# ---------------------------------------------------- lock, digest and misc surfaces


def test_lock_held_propagates_before_any_state(world: World) -> None:
    with clone_lock(world.repo, world.tmp / "locks"), pytest.raises(LockHeld):
        world.run()
    assert world.events == []
    assert not (world.tmp / "ws").exists()
    assert not (world.tmp / "digest-2026-01-02.md").exists()


def test_lock_held_by_caller_skips_locking(world: World) -> None:
    with clone_lock(world.repo, world.tmp / "locks"):
        result = world.run(lock_held=True)
    assert result.stages_run
    with clone_lock(world.repo, world.tmp / "locks"):
        pass


def test_default_still_locks_and_releases_on_error(world: World) -> None:
    with clone_lock(world.repo, world.tmp / "locks"), pytest.raises(LockHeld):
        world.run(lock_held=False)
    assert world.events == []


def test_lock_released_after_run(world: World) -> None:
    world.run()
    with clone_lock(world.repo, world.tmp / "locks"):
        pass


def test_digest_write_error_surfaces(world: World) -> None:
    blocker = world.tmp / "file"
    blocker.write_text("x")
    config = replace(world.config(), digest=DigestConfig(path_template=str(blocker / "d.md")))
    with pytest.raises(DigestWriteError):
        run_item(
            ITEM,
            config,
            world.options(),
            agent_runner=world.agent,
            subprocess_runner=world.shell,
            pr_client=world,
            clock=world.clock,
            today=TODAY,
        )


def test_digest_error_is_chained_to_stage_error(world: World) -> None:
    blocker = world.tmp / "file"
    blocker.write_text("x")
    world.ahead = "0"
    config = replace(world.config(), digest=DigestConfig(path_template=str(blocker / "d.md")))
    with pytest.raises(DigestWriteError) as info:
        run_item(
            ITEM,
            config,
            world.options(),
            agent_runner=world.agent,
            subprocess_runner=world.shell,
            pr_client=world,
            clock=world.clock,
            today=TODAY,
        )
    assert isinstance(info.value.__cause__, OrchestratorError)


def test_digest_can_be_deferred_to_the_caller(world: World) -> None:
    result = run_item(
        ITEM,
        world.config(),
        world.options(),
        agent_runner=world.agent,
        subprocess_runner=world.shell,
        pr_client=world,
        clock=world.clock,
        write_digest_file=False,
    )
    assert result.digest_path is None
    assert result.report.done


def test_required_config_is_validated(world: World) -> None:
    config = replace(world.config(), project=ProjectConfig())
    with pytest.raises(OrchestratorError, match="repo_dir"):
        run_item(
            ITEM,
            config,
            world.options(),
            agent_runner=world.agent,
            subprocess_runner=world.shell,
            pr_client=world,
        )
    with pytest.raises(OrchestratorError, match="base_branch"):
        world.run(base_branch="")


def test_branch_name_is_deterministic_and_safe() -> None:
    assert branch_name("ENG-1") == branch_name("ENG-1")
    assert branch_name("ENG-1") != branch_name("eng-1")
    assert re.fullmatch(r"sisyphus/eng-1-[0-9a-f]{8}", branch_name("ENG-1"))
    assert re.fullmatch(r"sisyphus/item-[0-9a-f]{8}", branch_name("###"))


def test_stop_report_path_sits_next_to_record(world: World) -> None:
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    assert stop_report_path(rec).parent == rec.path.parent


def test_default_subprocess_runner_captures_output(tmp_path: Path) -> None:
    res = default_subprocess_runner([sys.executable, "-c", "import sys; print('out'); sys.exit(3)"], tmp_path)
    assert (res.returncode, res.stdout.strip()) == (3, "out")
    assert default_subprocess_runner(["definitely-not-a-real-binary-xyz"], tmp_path).returncode == 127


def test_pr_client_protocol_is_satisfied_by_the_fake(world: World) -> None:
    client: orchestrator.PullRequestClient = world
    assert client.find_pr("b") is None


# -------------------------------------------------- AC7 / checklist: static guarantees


def _imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    names = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    return {n.split(".")[0] for n in names}


def test_orchestrator_does_not_import_harness_or_spec_prism_flow() -> None:
    assert not _imports(orchestrator.__file__) & {"harness", "spec_prism_flow"}
    text = Path(orchestrator.__file__).read_text()
    assert "import harness" not in text
    assert "spec_prism_flow" not in text


def test_tests_never_invoke_real_tools(world: World) -> None:
    world.merge_enabled = True
    world.pr_info = PrInfo(42, "MERGED", SHA1)
    world.run(ci_fix=True)
    for argv, _ in world.shell_calls:
        assert argv[0] in {"git", "harness", "make"}  # all through the fake, none executed for real
    assert world.events  # the fake saw every call


# ------------------------------------------------------------ AC10: prompt file

PROMPT = Path(find_checkout_root() or ".") / "knowledge" / "sisyphus" / "orchestration-prompt.md"
PARAMS = [
    "stage",
    "work-item reference",
    "brief path",
    "workspace dir",
    "target repo dir",
    "target branch",
    "phase config path",
    "cheat sheet path",
    "plan prompt path",
    "build prompt path",
    "max retry cycles",
    "stop report path",
]


def test_prompt_has_exactly_the_documented_placeholders() -> None:
    found = set(re.findall(r"<([^<>\n]+)>", PROMPT.read_text()))
    assert found == {*PARAMS, "leaf name"}  # "<leaf name>" is the handoffs/NN-<leaf name>.md convention


def test_prompt_substitution_leaves_no_parameter() -> None:
    text = build_orchestration_prompt(PROMPT.read_text(), {p: f"VALUE[{p}]" for p in PARAMS})
    assert set(re.findall(r"<([^<>\n]+)>", text)) == {"leaf name"}
    assert all(f"VALUE[{p}]" in text for p in PARAMS)


def test_prompt_is_tool_agnostic() -> None:
    text = PROMPT.read_text().lower()
    for word in (
        "claude",
        "anthropic",
        "codex",
        "cursor",
        "gemini",
        "copilot",
        "openai",
        "gpt",
        "harness",
        "spec-prism",
    ):
        assert word not in text
    assert re.search(r"\bgh\b", text) is None
    assert "build run" not in text


def test_prompt_references_but_does_not_restate() -> None:
    text = PROMPT.read_text()
    assert "Delegating plan and build to sub-agents" in " ".join(text.split())
    assert "plan prompt" in text.lower()
    assert "build prompt" in text.lower()
    root = find_checkout_root()
    assert root is not None
    sources = [
        root / "knowledge" / "spec-prism-flow" / "plan-generation-prompt.md",
        root / "knowledge" / "spec-prism-flow" / "build-execution-prompt.md",
        root / "tools" / "spec-prism-flow" / "docs" / "lifecycle-cheat-sheet.md",
    ]
    corpus = "\n".join(s.read_text() for s in sources)
    for line in text.splitlines():
        stripped = line.strip(" -*>")
        if len(stripped) > 40:
            assert stripped not in corpus, line


def test_prompt_states_the_required_behaviours() -> None:
    text = " ".join(PROMPT.read_text().split())
    for needle in ("handoffs/NN-<leaf name>.md", "one after the other", "never in parallel", "C3", "C4", "C5", "C2"):
        assert needle in text


# ------------------------------------------------- follow-up 18: stop record and resume


def _stop(world: World):
    from sisyphus.stoprecord import read_stop_record

    return read_stop_record(RunRecord.open(world.tmp / "ws", ITEM).path)


def _stop_c1(world: World) -> None:
    world.brief_text = BRIEF_OPEN


def _stop_c2(world: World) -> None:
    world.plan_stop = {"checkpoint": "C2", "leaf": "", "detail": "Which API version?"}


def _stop_c3(world: World) -> None:
    world.build_stops = [{"checkpoint": "C3", "leaf": "13-foo", "detail": "Use A or B?"}]


def _stop_c4(world: World) -> None:
    world.build_stops = [{"checkpoint": "C4", "leaf": "13-foo", "detail": "deviation"}]


def _stop_c5(world: World) -> None:
    world.gate_rc = 2


def _stop_c6(world: World) -> None:
    world.leaves = 5


def _stop_c7(world: World) -> None:
    world.remote_exists = True
    world.ancestor_rc = 1


def _stop_c8(world: World) -> None:
    world.fetch_rc = 1


@pytest.mark.parametrize(
    ("setup", "cp", "clears_by"),
    [
        (_stop_c1, "C1", "brief"),
        (_stop_c2, "C2", "answer"),
        (_stop_c3, "C3", "answer"),
        (_stop_c4, "C4", "answer"),
        (_stop_c5, "C5", "explicit"),
        (_stop_c6, "C6", "explicit"),
        (_stop_c7, "C7", "explicit"),
        (_stop_c8, "C8", "retry"),
    ],
)
def test_stop_record_written_for_each_checkpoint(world: World, setup, cp: str, clears_by: str) -> None:
    setup(world)
    world.run()
    stop = _stop(world)
    assert stop is not None
    assert (stop.kind, stop.checkpoint, stop.item, stop.clears_by) == ("checkpoint", cp, ITEM, clears_by)
    assert stop.stage is not None
    assert stop.questions
    assert stop.action
    assert stop.resume_command == f"sisyphus resume {ITEM} --config <your .sisyphus.toml>"
    line = _needs_you_lines(world.digest_text(), cp)[0]
    assert "Action:" in line
    assert f"Resume: `{stop.resume_command}`" in line


def test_c1_stop_record_lists_each_open_decision(world: World) -> None:
    world.brief_text = "# B\n\n## Open decisions log\n\n1. Which tracker?\n2. Which base?\n"
    world.run()
    stop = _stop(world)
    assert stop is not None
    assert stop.questions == ("1. Which tracker?", "2. Which base?")
    assert str(_brief(world)) in stop.action


def test_stop_record_written_for_an_unexpected_error(world: World) -> None:
    world.ahead = "0"
    with pytest.raises(OrchestratorError):
        world.run()
    stop = _stop(world)
    assert stop is not None
    assert (stop.kind, stop.checkpoint, stop.stage, stop.stage_status) == ("error", None, "build", "in_progress")
    assert "no commits" in stop.detail
    assert stop.clears_by == "explicit"
    line = _needs_you_lines(world.digest_text(), "error")[0]
    assert "no commits" in line
    assert "Resume:" in line


def test_stop_record_removed_when_the_run_completes(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    world.run()
    assert _stop(world) is not None
    _brief(world).write_text(BRIEF_NONE)
    assert world.run().completed
    assert _stop(world) is None


def test_options_resume_command_is_stored(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    world.run(resume_command="sisyphus resume {item} --config /c.toml --base b")
    stop = _stop(world)
    assert stop is not None
    assert stop.resume_command == "sisyphus resume ENG-1 --config /c.toml --base b"


def _brief(world: World) -> Path:
    from sisyphus.intake import resolve_brief_path

    return resolve_brief_path(world.config(), ITEM)


def test_c1_resume_with_edited_brief_proceeds_to_plan(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    world.run()
    _brief(world).write_text(BRIEF_NONE)
    result = world.run()
    assert result.completed
    assert world.events.count("agent:intake") == 1  # not redrafted
    assert world.events.count("agent:plan") == 1
    assert RunRecord.open(world.tmp / "ws", ITEM).checkpoint is None
    assert _stop(world) is None


def test_c1_resume_never_redrafts_a_human_edited_brief_even_if_the_prompt_changed(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    world.run()
    _brief(world).write_text(BRIEF_OPEN + "\n2. Resolved: ok\n")
    _brief(world).with_name("brief.md.intake.json").write_text(
        "{}"
    )  # prompt hash mismatch: a plain re-run would call the intake agent again
    result = world.run()
    assert result.checkpoint is Checkpoint.C1
    assert world.events == ["agent:intake"]


def test_c1_resume_without_edit_stops_again_with_same_question_and_repeats_nothing(world: World) -> None:
    world.brief_text = BRIEF_OPEN
    world.run()
    first = _stop(world)
    counts = world.counts()
    result = world.run()
    second = _stop(world)
    assert result.checkpoint is Checkpoint.C1
    assert first is not None
    assert second is not None
    assert second.questions == first.questions
    assert world.events == ["agent:intake"]
    assert world.counts()[0] == counts[0]  # no new agent call
    assert not any(c[0][:2] == ("git", "fetch") for c in world.shell_calls)


def test_resume_skips_a_stage_completed_before_a_between_stage_limit_stop(world: World) -> None:
    original = world.agent

    def slow(prompt: str) -> None:
        original(prompt)
        world.clock.now += 3 * 3600

    kw = {
        "agent_runner": slow,
        "subprocess_runner": world.shell,
        "pr_client": world,
        "clock": world.clock,
        "today": TODAY,
    }
    first = run_item(ITEM, world.config(), world.options(), **kw)  # ty: ignore[invalid-argument-type]
    assert first.checkpoint is Checkpoint.C6
    result = world.run()  # fresh clock budget (Clock keeps now, so reset)
    assert result.stages_skipped == ("intake",)
    assert world.events.count("agent:intake") == 1


def test_resume_keeps_retry_budgets_and_branch(world: World) -> None:
    world.build_stops = [{"checkpoint": "C5", "detail": "x"}]
    world.run()
    world.branch_exists = True
    world.run()
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    assert rec.retries["build:retry-cycles"] == 1
    assert rec.retries["branch-created"] == 1
    assert not any(c[0][:3] == ("git", "checkout", "-b") for c in world.shell_calls[-6:])


def test_answer_file_is_given_to_the_agent_once_then_consumed(world: World) -> None:
    from sisyphus.resume import answer_path

    world.build_stops = [{"checkpoint": "C3", "leaf": "13-foo", "detail": "A or B?"}]
    world.run()
    rec_path = RunRecord.open(world.tmp / "ws", ITEM).path
    answer_path(rec_path).write_text("Use B.\n")
    world.branch_exists = True
    assert world.run().completed
    build_prompts = [p for p in world.agent_prompts if "Stage to run now: `build`" in p]
    assert "Use B." in build_prompts[-1]
    assert "Use B." not in build_prompts[0]
    assert not answer_path(rec_path).exists()
    assert answer_path(rec_path).with_name(answer_path(rec_path).name + ".used").exists()


# ------------------------------------------------------ single dedicated clone


def test_dirty_clone_refuses_a_new_item_without_touching_it(world: World) -> None:
    world.dirty = " M tracked.txt\n"
    result = world.run()
    _assert_stop(world, result, Checkpoint.C8)
    assert "uncommitted" in result.detail
    assert world.events == []  # no agent run
    verbs = {c[0][1] for c in world.shell_calls if c[0][0] == "git"}
    assert verbs == {"status"}
    assert not verbs & {"stash", "reset", "clean", "checkout"}
    assert "uncommitted" in world.digest_text()


def test_dirty_clone_refuses_before_the_build_checkout(world: World) -> None:
    original = world.shell

    def dirty_after_plan(argv: Sequence[str], cwd: Path | None) -> CommandResult:
        if list(argv)[:2] == ["git", "status"] and "agent:plan" in world.events:
            return CommandResult(0, "?? leftover.txt\n")
        return original(argv, cwd)

    result = run_item(
        ITEM,
        world.config(),
        world.options(),
        agent_runner=world.agent,
        subprocess_runner=dirty_after_plan,
        pr_client=world,
        clock=world.clock,
        today=TODAY,
    )
    assert result.checkpoint is Checkpoint.C8
    assert "agent:build" not in world.events
    assert world.checkouts == []


def test_resume_checks_out_the_items_own_branch(world: World) -> None:
    world.push_rc = 1
    assert world.run().checkpoint is Checkpoint.C8  # stopped at push, branch committed
    world.push_rc = 0
    world.branch_exists = True
    world.current_branch = "sisyphus/other-item-12345678"  # another item ran meanwhile
    world.ancestor_rc_after_pr = 1  # nothing proves the branch deletable, so cleanup leaves the checkout alone
    result = world.run()
    assert result.completed
    assert world.checkouts[-1] == branch_name(ITEM)
    assert result.stages_skipped == ("intake", "plan", "build")
    assert world.events.count("agent:build") == 1  # committed work not redone


def test_resume_without_the_branch_is_an_error(world: World) -> None:
    world.push_rc = 1
    world.run()
    world.branch_exists = False
    world.current_branch = "elsewhere"
    with pytest.raises(OrchestratorError, match="does not exist"):
        world.run()


# ------------------------------------------------- local branch cleanup after a clean finish


def _cleanup_ready(world: World) -> None:
    world.branch_present_after_pr = True
    world.remote_exists = True


def test_clean_finish_deletes_the_branch_with_d_after_switching_to_base(world: World) -> None:
    _cleanup_ready(world)
    result = world.run()
    assert result.completed
    assert world.branch_cmds == [
        ["branch", f"--set-upstream-to=origin/{branch_name(ITEM)}", branch_name(ITEM)],
        ["branch", "-d", branch_name(ITEM)],
    ]
    assert world.current_branch == "base-branch"
    calls = [c[0] for c in world.shell_calls]
    assert calls.index(("git", "checkout", "base-branch")) < calls.index(("git", "branch", "-d", branch_name(ITEM)))
    assert ("git", "merge", "--ff-only", "refs/remotes/origin/base-branch") in calls
    assert f"local branch {branch_name(ITEM)} deleted" in world.digest_text()


def test_capital_d_is_used_only_on_the_proof_3_path_and_never_with_force(world: World) -> None:
    _cleanup_ready(world)  # proof 1 holds: plain -d, gh not asked
    world.run()
    assert not any("-D" in argv or "--force" in argv for argv, _ in world.shell_calls)
    assert world.pr_state_refs == []


def test_branch_already_gone_is_silent_and_idempotent(world: World) -> None:
    _cleanup_ready(world)
    world.run()
    before = len(world.shell_calls)
    result = world.run()
    assert result.completed
    assert [c[0] for c in world.shell_calls[before:]] == [
        ("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name(ITEM)}")
    ]
    assert "Branch kept" not in world.digest_text()
    assert "deleted" not in world.digest_text()


def test_not_pushed_keeps_the_branch_and_notes_it(world: World) -> None:
    world.branch_present_after_pr = True
    world.remote_exists = False
    world.ancestor_rc_after_pr = 1  # not on origin/<base> either
    world.run()
    assert world.branch_cmds == []
    text = world.digest_text()
    assert f"Branch kept: {ITEM} {branch_name(ITEM)} no deletion proof held: (1) not pushed" in text
    assert "(2) head is not an ancestor of origin/base-branch" in text
    assert "(3) no PR found for 42" in text  # the run record stores the PR number


def test_done_line_names_the_proof(world: World) -> None:
    _cleanup_ready(world)
    world.run()
    assert f"deleted (proof 1: head {SHA1[:12]} is an ancestor of origin/{branch_name(ITEM)})" in world.digest_text()
    assert world.pr_state_refs == []  # gh is not asked when git proves it


def test_remote_branch_gone_after_merge_is_proven_against_base(world: World) -> None:
    world.branch_present_after_pr = True
    world.remote_exists = False
    world.ancestor_rc_after_pr = 1
    world.run()
    world.ancestor_rc_after_pr = 0  # merged into origin/base-branch, remote branch deleted
    world.pr_state_refs.clear()
    world.branch_present_after_pr = True
    world.run()
    assert ["branch", "-d", branch_name(ITEM)] in world.branch_cmds
    assert not any(c[1].startswith("--set-upstream") for c in world.branch_cmds)
    assert "proof 2: head" in world.digest_text()
    assert world.pr_state_refs == []


def test_squash_merge_proof_uses_the_stored_pr_number_and_gh_error_keeps(world: World) -> None:
    world.branch_present_after_pr = True
    world.remote_exists = False
    world.ancestor_rc_after_pr = 1
    world.pr_state_error = GitHubError("gh pr view failed (exit 1): boom")
    world.run()
    assert world.pr_state_refs == ["42"]
    assert world.branch_cmds == []
    assert "(3) gh lookup of 42 failed: gh pr view failed (exit 1): boom" in world.digest_text()


def test_commits_not_on_origin_keep_the_branch(world: World) -> None:
    _cleanup_ready(world)
    world.ancestor_rc_after_pr = 1
    world.ahead = "2"
    world.run()
    assert world.branch_cmds == []
    assert "has 2 commit(s) not on origin" in world.digest_text()


def test_dirty_clone_keeps_the_branch_and_touches_nothing(world: World) -> None:
    _cleanup_ready(world)
    world.run()  # deleted; now a second item state: a dirty clone on a re-created branch
    world.branch_present_after_pr = True
    world.dirty = " M tracked.txt\n"
    before = len(world.shell_calls)
    world.run()
    new = [c[0] for c in world.shell_calls[before:]]
    assert not any(c[:2] in (("git", "checkout"), ("git", "branch"), ("git", "merge")) for c in new)
    assert not any(c[1] in ("stash", "reset", "clean") for c in new)
    assert "the clone is dirty" in world.digest_text()


def test_failed_base_fast_forward_keeps_the_branch(world: World) -> None:
    _cleanup_ready(world)
    world.ff_rc = 1
    world.run()
    assert world.branch_cmds == []
    assert "cannot be fast-forwarded" in world.digest_text()


@pytest.mark.parametrize("fault", ["gate", "c1", "error"])
def test_stopped_or_failed_item_keeps_its_branch(world: World, fault: str) -> None:
    _cleanup_ready(world)
    if fault == "gate":
        world.gate_rc = 1
    elif fault == "c1":
        world.brief_text = BRIEF_OPEN
    else:
        world.ahead = "0"  # build produced no commits: OrchestratorError
    if fault == "error":
        with pytest.raises(OrchestratorError):
            world.run()
    else:
        assert not world.run().completed
    assert world.branch_cmds == []
    assert not any(c[0][:2] == ("git", "checkout") and c[0][2] == "base-branch" for c in world.shell_calls)


def test_abandoned_item_keeps_its_branch(world: World) -> None:
    world.run()
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    write_stop_record(
        rec.path,
        build_abandoned_record(ITEM, stage="open-pr", stage_status="complete", prior_label="C7", reenable_command="x"),
    )
    _cleanup_ready(world)
    world.run()
    assert world.branch_cmds == []


def test_ci_fix_after_branch_deleted_works_from_the_remote_head(world: World) -> None:
    _cleanup_ready(world)
    world.run()
    assert world.branch_present_after_pr is False
    before = len(world.shell_calls)
    result = world.run(ci_fix=True)
    assert result.completed
    assert result.stages_run == ("ci-fix",)
    new = [c[0] for c in world.shell_calls[before:]]
    assert ("git", "checkout", "--detach", "FETCH_HEAD") in new
    assert ("harness", "run", "ci-fix", "--pr", "42") in new
    assert not any(c[:3] == ("git", "checkout", branch_name(ITEM)) for c in new)


def test_merge_after_branch_deleted_works_from_the_remote_head(world: World) -> None:
    _cleanup_ready(world)
    world.run()
    world.merge_enabled = True
    world.pr_info = PrInfo(42, "MERGED", SHA1)
    result = world.run()
    assert result.completed
    assert result.stages_run == ("merge",)


def test_resume_before_the_pr_stage_still_needs_the_local_branch(world: World) -> None:
    world.run()
    rec = RunRecord.open(world.tmp / "ws", ITEM)
    rec.mark_stage("push", "in_progress")
    world.current_branch = "base-branch"
    with pytest.raises(OrchestratorError, match="does not exist"):
        world.run()


REQUIREMENTS = (
    "# Requirements\n\n### 1. Overview\n\nCC @someone about #7 in {ws}.\n\n### 5. Glossary\n\nGlossary text.\n"
)


def _header(item: str = ITEM) -> str:
    return f"Work item: {item}\n\nOpened by Sisyphus. Review required; this PR is not approved."


def test_open_pr_body_is_assembled(world: World) -> None:
    world.requirements = REQUIREMENTS.replace("{ws}", str(world.tmp / "ws"))
    world.git_log = "abc1234\t00-leaf0-leaf done\n"
    handoffs = world.tmp / "ws" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "00-leaf0-leaf.md").write_text("### Handoff\n- Deviations from the plan: Used a ZIP.\n")
    result = world.run()
    assert result.completed
    body = world.opened[0][3]
    assert body.startswith(_header())
    order = [body.index(t) for t in ("## Summary", "## Phases", "## Deviations", "<details>")]
    assert order == sorted(order)
    assert "abc1234" in body
    assert "Used a ZIP" in body
    assert "Glossary text." in body
    assert str(world.tmp) not in body
    assert not pick.mentions_issue(body, 7)
    assert "@someone" not in body
    assert world.log_calls == [["log", "--reverse", "--format=%h%x09%s", "origin/base-branch..HEAD"]]


def test_open_pr_body_missing_artifacts_still_opens(world: World) -> None:
    world.git_log_rc = 1
    result = world.run()
    assert result.completed
    assert result.pr == PullRequest(42, "https://example.test/pr/42")
    body = world.opened[0][3]
    assert body.startswith(_header())
    assert "requirements.md" in body  # one-line note instead of the file


def test_open_pr_body_oversize_is_capped(world: World) -> None:
    world.requirements = "# Requirements\n\n### 1. Overview\n\n" + "word " * 20000
    world.run()
    body = world.opened[0][3]
    assert len(body) <= 60000
    assert body.startswith(_header())


def test_assembly_failure_does_not_stop_the_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator.prbody, "compose_body", boom)
    result = world.run()
    assert result.completed
    assert world.opened[0][3] == _header()


def test_existing_pr_does_not_assemble_a_body(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.existing_pr = PullRequest(42, "https://example.test/pr/42")
    calls: list[str] = []
    monkeypatch.setattr(orchestrator.prbody, "compose_body", lambda *a, **k: calls.append("x") or "")
    world.run()
    assert world.opened == []
    assert calls == []
    assert world.log_calls == []
