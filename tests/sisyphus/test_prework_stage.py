"""``_Run.stage_prework``: health pass, pattern pass, gate, failure and resume."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from dotharness.sisyphus import orchestrator
from dotharness.sisyphus.orchestrator import (
    GATE_FAILED_KEY,
    PREWORK_INPUT_FILE,
    PREWORK_RETRY_KEY,
    REFACTOR_INPUT_FILE,
    REFACTOR_RETRY_KEY,
    _Stop,
)
from dotharness.sisyphus.rails import Checkpoint
from dotharness.sisyphus.stack import StackGroup
from dotharness.sisyphus.subproc import CommandResult
from tests.sisyphus.test_orchestrator import ITEM, World


class Fake:
    """Scripted git, vibe_heal and gate commands with a mutable branch, HEAD and dirty state."""

    def __init__(self) -> None:
        self.branch = "main"
        self.head = "h0"
        self.dirty = ""
        self.diff = ""
        self.commits = 0
        self.gate_rc = 0
        self.vh_rc: dict[tuple[str, str], int] = {}
        self.branch_exists = False
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:  # noqa: C901
        a = tuple(argv)
        self.calls.append(a)
        if a[0] == "vibe_heal":
            rc = self.vh_rc.get((a[1], a[2]), 0)
            return CommandResult(rc, "", "vh boom" if rc else "")
        if a[0] != "git":
            return CommandResult(self.gate_rc, "", "gate boom" if self.gate_rc else "")
        if a[1] == "fetch":
            return CommandResult(0, "", "")
        if a[1:3] == ("rev-parse", "--verify"):
            return CommandResult(0 if self.branch_exists else 1, "", "")
        if a[1] == "checkout":
            self.branch = a[3] if a[2] == "-b" else a[2]
            return CommandResult(0, "", "")
        if a[1:] == ("rev-parse", "HEAD"):
            return CommandResult(0, f"{self.head}\n", "")
        if a[1:] == ("rev-parse", "--abbrev-ref", "HEAD"):
            return CommandResult(0, f"{self.branch}\n", "")
        if a[1] == "status":
            return CommandResult(0, self.dirty, "")
        if a[1] == "diff":
            return CommandResult(0, self.diff, "")
        if a[1] == "rev-list":
            return CommandResult(0, f"{self.commits}\n", "")
        raise AssertionError(a)


def G(name: str, files: tuple[str, ...] = ("a.py",), tests: tuple[str, ...] = ()) -> StackGroup:
    return StackGroup(set=name, files=files, tests=tests, evidence=(), fallback=name == "generic")


class Env:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.world = World(tmp_path)
        w = self.world
        self.fake = Fake()
        self.run = orchestrator._Run(ITEM, w.config(), w.options(), w.agent, self.fake, w, w.clock, w)
        self.groups: list[StackGroup] = []
        self.agent_calls: list[dict[str, Any]] = []
        self.on_agent: Callable[[str], None] = lambda s: None
        self.write_handoff = True
        self.reason: str | None = None
        self.plan: list[tuple[Path, str]] = [(tmp_path, "make check")]
        self.plan_calls = 0
        self.times: list[str] = []
        self.pattern_sets: list[str | None] = []
        run = self.run
        run.prework_files = ("a.py", "b.py")
        run.prework_vibeheal = True

        def group_(repo: Path, files: Sequence[str], pattern_set: str | None = None) -> tuple[StackGroup, ...]:
            self.pattern_sets.append(pattern_set)
            return tuple(self.groups)

        async def agent(stage: str, **kw: Any) -> None:
            assert stage == "prework"
            self.agent_calls.append(kw)
            s = kw["extra_values"]["refactor_set"]
            if self.write_handoff:
                run.handoff_dir.mkdir(parents=True, exist_ok=True)
                run.prework_handoff_path(s).write_text("- Edits to existing files: none\n", encoding="utf-8")
            self.on_agent(s)

        async def plan_() -> tuple[list[tuple[Path, str]], str | None]:
            self.plan_calls += 1
            return self.plan, self.reason

        monkeypatch.setattr(orchestrator, "group_added_files", group_)
        monkeypatch.setattr(run, "run_agent_stage", agent)
        monkeypatch.setattr(run, "build_gate_plan", plan_)
        monkeypatch.setattr(run, "check_time", lambda: self.times.append("t"))

    def retries(self) -> int:
        return self.run.rec.retries.get(PREWORK_RETRY_KEY, 0)

    def vibe_calls(self) -> list[tuple[str, ...]]:
        return [c for c in self.fake.calls if c[0] == "vibe_heal"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


async def _stop(env: Env) -> _Stop:
    with pytest.raises(_Stop) as exc:
        await env.run.stage_prework()
    return exc.value


async def test_empty_set_completes_without_calls(env: Env) -> None:
    env.run.prework_files = ()
    await env.run.stage_prework()
    assert "prework: no existing source files in the plan scope" in env.run.notes
    assert env.fake.calls == []
    assert env.agent_calls == []
    assert env.run.rec.stage == "prework"
    assert env.run.rec.stage_status == "complete"


async def test_branch_is_cut_from_base_without_push(env: Env) -> None:
    await env.run.stage_prework()
    base = env.run.opt.base_branch
    assert ("git", "fetch", "origin", base) in env.fake.calls
    assert ("git", "checkout", "-b", f"{env.run.branch}-prework", f"origin/{base}") in env.fake.calls
    assert not [c for c in env.fake.calls if c[:2] == ("git", "push")]
    assert env.run.prework_branch == f"{env.run.branch}-prework"


async def test_resume_checks_out_existing_branch(env: Env) -> None:
    env.fake.branch_exists = True
    await env.run.stage_prework()
    assert ("git", "checkout", env.run.prework_branch) in env.fake.calls
    assert not [c for c in env.fake.calls if c[:3] == ("git", "checkout", "-b")]


async def test_health_pass_order_dedupe_then_fix_per_file(env: Env) -> None:
    await env.run.stage_prework()
    assert env.vibe_calls() == [
        ("vibe_heal", "dedupe", "a.py"),
        ("vibe_heal", "fix", "a.py"),
        ("vibe_heal", "dedupe", "b.py"),
        ("vibe_heal", "fix", "b.py"),
    ]
    assert env.times == ["t"]  # between the two files only


async def test_flag_off_skips_health_pass_and_runs_pattern_pass(env: Env) -> None:
    env.run.prework_vibeheal = False
    env.groups = [G("generic")]
    await env.run.stage_prework()
    assert "prework: vibe_heal not configured" in env.run.notes
    assert env.vibe_calls() == []
    assert len(env.agent_calls) == 1


@pytest.mark.parametrize(("cmd", "file"), [("dedupe", "a.py"), ("fix", "a.py"), ("fix", "b.py")])
async def test_nonzero_exit_stops_at_c5(env: Env, cmd: str, file: str) -> None:
    env.fake.vh_rc[(cmd, file)] = 3
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert f"vibe_heal {cmd} failed for {file} (exit 3): vh boom" in stop.detail
    assert env.retries() == 1
    assert env.agent_calls == []
    assert env.vibe_calls()[-1] == ("vibe_heal", cmd, file)


async def test_escalation_after_three_cycles(env: Env) -> None:
    env.run.rec.retries[PREWORK_RETRY_KEY] = 3
    env.run.rec.save()
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert stop.detail == "prework already failed 3 retry cycles; not launching again"
    assert env.fake.calls == []
    assert env.retries() == 3


async def test_escalation_check_precedes_empty_set(env: Env) -> None:
    env.run.prework_files = ()
    env.run.rec.retries[PREWORK_RETRY_KEY] = 3
    env.run.rec.save()
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5


async def test_pattern_pass_call_shape_and_input_file(env: Env) -> None:
    env.groups = [G("fastapi", files=("a.py", "b.py"))]
    await env.run.stage_prework()
    kw = env.agent_calls[0]
    assert kw["prompt_file"] == orchestrator.REFACTOR_PROMPT_FILE
    extra = kw["extra_values"]
    assert extra["refactor_set"] == "fastapi"
    assert extra["group_files"] == "a.py\nb.py"
    assert extra["modified_files"] == "a.py\nb.py"
    assert extra["input_file"] == str(env.run.prework_input_path)
    assert extra["target branch"] == env.run.prework_branch
    assert extra["handoff_prefix"] == "prework"
    assert env.run.prework_input_path.name == PREWORK_INPUT_FILE
    entry = env.run.read_refactor_input(env.run.prework_input_path)[0]
    assert entry["set"] == "fastapi"
    assert entry["head_before"] == "h0"
    assert entry["files"] == ["a.py", "b.py"]
    assert env.pattern_sets == [None]


async def test_refactor_input_file_untouched(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.stage_prework()
    assert env.run.prework_input_path.exists()
    assert not (env.run.handoff_dir / REFACTOR_INPUT_FILE).exists()
    assert env.run.read_refactor_input() == []


async def test_handoff_paths_do_not_collide(env: Env) -> None:
    assert env.run.prework_handoff_path("fastapi") != env.run.refactor_handoff_path("fastapi")


async def test_no_groups_notes_and_gates(env: Env) -> None:
    await env.run.stage_prework()
    assert "prework: no stack groups in the pre-work set" in env.run.notes
    assert env.agent_calls == []


async def test_check_time_between_groups(env: Env) -> None:
    env.run.prework_vibeheal = False
    env.groups = [G("a"), G("b"), G("c")]
    await env.run.stage_prework()
    assert env.times == ["t", "t"]


async def test_head_before_per_group(env: Env) -> None:
    env.run.prework_vibeheal = False
    env.groups = [G("a"), G("b")]

    def commit(_: str) -> None:
        env.fake.head = f"{env.fake.head}x"

    env.on_agent = commit
    await env.run.stage_prework()
    heads = {e["set"]: e["head_before"] for e in env.run.read_refactor_input(env.run.prework_input_path)}
    assert heads == {"a": "h0", "b": "h0x"}


async def test_no_commits_no_gate(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.stage_prework()
    assert env.plan_calls == 0


async def test_gate_runs_once_with_commits(env: Env) -> None:
    env.groups = [G("a"), G("b")]
    env.fake.commits = 2
    await env.run.stage_prework()
    assert env.plan_calls == 1
    assert env.fake.calls.count(("make", "check")) == 1
    assert ("git", "rev-list", "--count", f"origin/{env.run.opt.base_branch}..HEAD") in env.fake.calls
    assert env.retries() == 0


async def test_gate_not_run_note(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 1
    env.reason = "no gate configured"
    await env.run.stage_prework()
    assert "prework gate not run: no gate configured" in env.run.notes


async def test_gate_failure_stops_at_c5_and_keeps_commits(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 1
    env.fake.gate_rc = 2
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert "prework gate failed: 'make check' in " in stop.detail
    assert "(exit 2): gate boom" in stop.detail
    assert env.retries() == 1
    assert env.run.rec.retries.get(REFACTOR_RETRY_KEY, 0) == 0
    assert env.run.rec.retries[GATE_FAILED_KEY] == 1
    assert not [c for c in env.fake.calls if c[:2] == ("git", "reset")]


async def test_passing_gate_clears_stale_failure_flag(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 1
    env.run.rec.retries[GATE_FAILED_KEY] = 1
    env.run.rec.save()
    await env.run.stage_prework()
    assert env.run.rec.retries[GATE_FAILED_KEY] == 0


@pytest.mark.parametrize("problem", ["branch", "dirty", "handoff", "scope"])
async def test_check_failures_stop_at_c5(env: Env, problem: str) -> None:
    env.groups = [G("generic")]
    if problem == "branch":
        env.on_agent = lambda s: setattr(env.fake, "branch", "other")
    elif problem == "dirty":
        env.on_agent = lambda s: setattr(env.fake, "dirty", " M a.py")
    elif problem == "handoff":
        env.write_handoff = False
    else:
        env.on_agent = lambda s: setattr(env.fake, "diff", "M\0other.py\0")
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert "the prework agent ended before group generic was complete" in stop.detail
    assert env.retries() == 1
    assert env.run.rec.retries.get(REFACTOR_RETRY_KEY, 0) == 0
    if problem == "branch":
        assert f"expected {env.run.prework_branch!r}" in stop.detail
    if problem == "scope":
        assert "other.py" in stop.detail


async def test_resume_skips_complete_group_and_reruns_gate(env: Env) -> None:
    env.run.prework_vibeheal = False
    env.groups = [G("generic")]
    env.fake.commits = 1
    env.fake.gate_rc = 2
    await _stop(env)
    assert len(env.agent_calls) == 1
    env.fake.gate_rc = 0
    env.fake.branch_exists = True
    await env.run.stage_prework()
    assert len(env.agent_calls) == 1  # skipped: handoff and entry exist, tree clean
    assert env.fake.calls.count(("make", "check")) == 2  # only the gate re-ran
    assert len(env.run.read_refactor_input(env.run.prework_input_path)) == 1


async def test_resume_without_handoff_reruns_group_keeping_entry(env: Env) -> None:
    env.run.prework_vibeheal = False
    env.groups = [G("generic")]
    env.write_handoff = False
    await _stop(env)
    env.write_handoff = True
    env.fake.branch_exists = True
    await env.run.stage_prework()
    assert len(env.agent_calls) == 2
    assert len(env.run.read_refactor_input(env.run.prework_input_path)) == 1


async def test_dirty_tree_at_stage_start_is_c8(env: Env) -> None:
    env.fake.dirty = " M a.py"
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C8
    assert env.vibe_calls() == []


async def test_completed_stage_rerun_makes_no_new_commits_or_entries(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.stage_prework()
    before = env.run.prework_input_path.read_text(encoding="utf-8")
    agents = len(env.agent_calls)
    env.fake.branch_exists = True
    await env.run.stage_prework()
    assert env.run.prework_input_path.read_text(encoding="utf-8") == before
    assert len(env.agent_calls) == agents
    assert not [c for c in env.fake.calls if c[:2] == ("git", "commit")]


async def test_stop_report_c5_increments_prework_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: Env) -> None:
    stop_file = tmp_path / "stop.json"
    stop_file.write_text('{"checkpoint": "C5", "detail": "d"}')
    with pytest.raises(_Stop) as exc:
        env.run.raise_stop_report("prework", stop_file)
    assert exc.value.checkpoint == Checkpoint.C5
    assert env.retries() == 1
    assert env.run.rec.retries.get(REFACTOR_RETRY_KEY, 0) == 0
    stop_file.write_text('{"checkpoint": "C3", "detail": "d"}')
    with pytest.raises(_Stop):
        env.run.raise_stop_report("prework", stop_file)
    assert env.retries() == 1
    stop_file.write_text('{"checkpoint": "C2", "detail": "d"}')
    with pytest.raises(orchestrator.OrchestratorError):
        env.run.raise_stop_report("prework", stop_file)


async def test_disabled_in_config_is_skipped(tmp_path: Path) -> None:
    from dataclasses import replace

    from dotharness.sisyphus.config import PreworkConfig

    w = World(tmp_path)
    config = replace(w.config(), prework=PreworkConfig(enabled=False))
    run = orchestrator._Run(ITEM, config, w.options(), w.agent, w.shell, w, w.clock, w)
    await run.execute()
    assert "prework" in run.skipped
    assert "prework" not in run.done_stages
