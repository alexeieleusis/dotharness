"""``_Run.stage_refactor``: the per-group loop, completion and scope check, and the gate call."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from dotharness.sisyphus import orchestrator
from dotharness.sisyphus.orchestrator import GATE_FAILED_KEY, REFACTOR_RETRY_KEY, OrchestratorError, _Stop
from dotharness.sisyphus.rails import Checkpoint
from dotharness.sisyphus.stack import StackGroup
from dotharness.sisyphus.subproc import CommandResult
from tests.sisyphus.test_orchestrator import ITEM, World


class Fake:
    """Scripted git plus gate commands, with a mutable HEAD, branch and dirty state."""

    def __init__(self, branch: str) -> None:
        self.branch = branch
        self.head = "h0"
        self.dirty = ""
        self.diff = ""
        self.commits = 0
        self.gate_rc = 0
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: Sequence[str], cwd: Path | None) -> CommandResult:
        a = tuple(argv)
        self.calls.append(a)
        if a[0] != "git":
            return CommandResult(self.gate_rc, "", "gate boom" if self.gate_rc else "")
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
        self.fake = Fake("")
        self.run = orchestrator._Run(ITEM, w.config(), w.options(), w.agent, self.fake, w, w.clock, w)
        self.fake.branch = self.run.branch
        self.groups: list[StackGroup] = []
        self.agent_calls: list[dict[str, Any]] = []
        self.on_agent: Callable[[str], None] = lambda s: None
        self.write_handoff = True
        self.handoff_text = "- Edits to existing files: none\n"
        self.prepared = 0
        self.plan_calls = 0
        self.reason: str | None = None
        self.plan: list[tuple[Path, str]] = [(tmp_path, "make check")]
        self.times: list[str] = []
        self.heads_at_agent: list[str] = []
        self.entries_at_agent: list[list[str]] = []
        run = self.run

        async def groups_() -> tuple[StackGroup, ...]:
            return tuple(self.groups)

        async def prepare() -> None:
            self.prepared += 1

        async def agent(stage: str, **kw: Any) -> None:
            assert stage == "refactor"
            self.agent_calls.append(kw)
            s = kw["extra_values"]["refactor_set"]
            self.heads_at_agent.append(self.fake.head)
            self.entries_at_agent.append([e["set"] for e in run.read_refactor_input()])
            if self.write_handoff:
                run.handoff_dir.mkdir(parents=True, exist_ok=True)
                run.refactor_handoff_path(s).write_text(self.handoff_text, encoding="utf-8")
            self.on_agent(s)

        async def plan_() -> tuple[list[tuple[Path, str]], str | None]:
            self.plan_calls += 1
            return self.plan, self.reason

        monkeypatch.setattr(run, "refactor_groups", groups_)
        monkeypatch.setattr(run, "prepare_branch", prepare)
        monkeypatch.setattr(run, "run_agent_stage", agent)
        monkeypatch.setattr(run, "build_gate_plan", plan_)
        monkeypatch.setattr(run, "check_time", lambda: self.times.append("t"))

    def retries(self) -> int:
        return self.run.rec.retries.get(REFACTOR_RETRY_KEY, 0)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(tmp_path, monkeypatch)


async def _stop(env: Env) -> _Stop:
    with pytest.raises(_Stop) as exc:
        await env.run.stage_refactor()
    return exc.value


async def test_t1_no_files(env: Env) -> None:
    await env.run.stage_refactor()
    assert "refactor: no added source files" in env.run.notes
    assert env.agent_calls == []
    assert env.prepared == 1
    assert env.run.rec.stage == "refactor"
    assert env.run.rec.stage_status == "complete"


async def test_empty_group_dropped(env: Env) -> None:
    env.groups = [G("fastapi", files=())]
    await env.run.stage_refactor()
    assert env.agent_calls == []
    assert "refactor: no added source files" in env.run.notes


async def test_t2_no_commits_no_gate(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.stage_refactor()
    assert len(env.agent_calls) == 1
    assert env.plan_calls == 0
    kw = env.agent_calls[0]
    assert kw["prompt_file"] == orchestrator.REFACTOR_PROMPT_FILE
    assert kw["extra_values"] == {
        "refactor_set": "generic",
        "group_files": "a.py",
        "input_file": str(env.run.refactor_input_path),
    }


async def test_group_files_newline_joined(env: Env) -> None:
    env.groups = [G("generic", files=("a.py", "b.py"))]
    await env.run.stage_refactor()
    assert env.agent_calls[0]["extra_values"]["group_files"] == "a.py\nb.py"


async def test_t3_passing_gate(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 2
    await env.run.stage_refactor()
    assert env.plan_calls == 1
    assert ("make", "check") in env.fake.calls
    assert env.retries() == 0
    assert GATE_FAILED_KEY not in env.run.rec.retries
    assert not env.run.gated
    assert ("git", "rev-list", "--count", "h0..HEAD") in env.fake.calls


async def test_t4_gate_not_run(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 1
    env.reason = "no gate configured"
    await env.run.stage_refactor()
    assert "refactor gate not run: no gate configured" in env.run.notes
    assert ("make", "check") not in env.fake.calls


async def test_t5_failing_gate(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 1
    env.fake.gate_rc = 2
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert "refactor gate failed: 'make check' in " in stop.detail
    assert "(exit 2): gate boom" in stop.detail
    assert env.retries() == 1
    assert env.run.rec.retries[GATE_FAILED_KEY] == 1


async def test_gate_failure_none_is_defensive(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.groups = [G("generic")]
    env.fake.commits = 1

    async def failed(plan: Any) -> bool:
        return True

    monkeypatch.setattr(env.run, "run_gate_plan", failed)
    stop = await _stop(env)
    assert stop.detail == "refactor gate failed"
    assert env.retries() == 1


async def test_t6_two_groups_head_before(env: Env) -> None:
    env.groups = [G("fastapi"), G("generic", files=("b.py",))]

    def commit(s: str) -> None:
        if s == "fastapi":
            env.fake.head = "h1"
            env.fake.commits = 1

    env.on_agent = commit
    await env.run.stage_refactor()
    assert env.entries_at_agent == [["fastapi"], ["fastapi", "generic"]]  # a group is recorded just before its call
    heads = {e["set"]: e["head_before"] for e in env.run.read_refactor_input()}
    assert heads == {"fastapi": "h0", "generic": "h1"}
    assert env.heads_at_agent == ["h0", "h1"]


async def test_t7_all_problems_one_message(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.dirty = "?? x.py\n"
    env.fake.branch = "elsewhere"
    env.write_handoff = False
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert stop.detail.startswith("the refactor agent ended before group generic was complete; ")
    assert "checkout is on 'elsewhere', expected " in stop.detail
    assert "uncommitted changes: ?? x.py" in stop.detail
    assert "no handoff for set generic" in stop.detail
    assert "out-of-scope" not in stop.detail
    assert not any(c[1] == "diff" for c in env.fake.calls)
    assert env.retries() == 1


@pytest.mark.parametrize("problem", ["dirty", "branch", "handoff"])
async def test_t7_each_problem_alone(env: Env, problem: str) -> None:
    env.groups = [G("generic")]
    if problem == "dirty":
        env.fake.dirty = " M a.py\n"
    elif problem == "branch":
        env.fake.branch = "other"
    else:
        env.write_handoff = False
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert "; " not in stop.detail.split("complete; ", 1)[1]
    assert env.retries() == 1


async def test_t8_scope_violation(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.diff = "M\0other.py\0D\0gone.py\0A\0new.py\0"
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert "out-of-scope edits: M other.py, D gone.py" in stop.detail
    assert ("git", "diff", "--no-renames", "--name-status", "-z", "h0..HEAD") in env.fake.calls
    assert env.retries() == 1


async def test_scope_honours_listed_edits(env: Env) -> None:
    env.groups = [G("generic")]
    env.fake.diff = "M\0other.py\0"
    env.handoff_text = "- Edits to existing files: other.py (needed)\n"
    await env.run.stage_refactor()
    assert env.retries() == 0


async def test_t9_check_time_between_groups(env: Env) -> None:
    env.groups = [G("a"), G("b", files=("b.py",)), G("generic", files=("c.py",))]
    await env.run.stage_refactor()
    assert len(env.times) == 2


async def test_t9_not_before_first(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.stage_refactor()
    assert env.times == []


async def test_t10_budget_spent(env: Env) -> None:
    env.run.rec.retries[REFACTOR_RETRY_KEY] = 3
    env.groups = [G("generic")]
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C5
    assert stop.detail == "refactor already failed 3 retry cycles; not launching again"
    assert env.prepared == 0
    assert env.agent_calls == []


async def test_t11_resume_skips_agent(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.record_group_entry(env.groups[0])
    env.run.refactor_handoff_path("generic").write_text("- Edits to existing files: none\n", encoding="utf-8")
    env.fake.head = "h9"
    env.fake.commits = 1
    await env.run.stage_refactor()
    assert env.agent_calls == []
    assert env.run.read_refactor_input()[0]["head_before"] == "h0"  # recorded before HEAD moved, kept
    assert ("git", "rev-list", "--count", "h0..HEAD") in env.fake.calls
    assert ("make", "check") in env.fake.calls


async def test_t11_resume_skip_reruns_scope_check(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.record_group_entry(env.groups[0])
    env.run.refactor_handoff_path("generic").write_text("x\n", encoding="utf-8")
    env.fake.diff = "M\0other.py\0"
    stop = await _stop(env)
    assert "out-of-scope edits" in stop.detail
    assert env.agent_calls == []


async def test_t12_resume_dirty_reruns_agent(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.record_group_entry(env.groups[0])
    env.run.refactor_handoff_path("generic").write_text("x\n", encoding="utf-8")
    env.fake.dirty = " M a.py\n"

    def clean(s: str) -> None:
        env.fake.dirty = ""

    env.on_agent = clean
    env.fake.head = "h5"
    await env.run.stage_refactor()
    assert len(env.agent_calls) == 1
    assert env.run.read_refactor_input()[0]["head_before"] == "h0"


async def test_t12_resume_missing_handoff_reruns_agent(env: Env) -> None:
    env.groups = [G("generic")]
    await env.run.record_group_entry(env.groups[0])
    await env.run.stage_refactor()
    assert len(env.agent_calls) == 1


async def test_t13_stop_ends_stage(env: Env) -> None:
    env.groups = [G("fastapi"), G("generic", files=("b.py",))]
    env.write_handoff = False
    await _stop(env)
    assert len(env.agent_calls) == 1
    assert [e["set"] for e in env.run.read_refactor_input()] == ["fastapi"]


async def test_agent_stop_propagates(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.groups = [G("fastapi"), G("generic", files=("b.py",))]

    async def boom(stage: str, **kw: Any) -> None:
        raise _Stop(Checkpoint.C3, "need an answer")

    monkeypatch.setattr(env.run, "run_agent_stage", boom)
    stop = await _stop(env)
    assert stop.checkpoint == Checkpoint.C3
    assert env.retries() == 0


async def test_no_no_commits_error(env: Env) -> None:
    env.groups = [G("generic")]
    try:
        await env.run.stage_refactor()
    except OrchestratorError as exc:  # pragma: no cover
        pytest.fail(str(exc))
