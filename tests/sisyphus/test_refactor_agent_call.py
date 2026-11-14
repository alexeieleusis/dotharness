"""``_Run.run_agent_stage``: prompt file and extra values for the refactor stage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dotharness.sisyphus import orchestrator
from dotharness.sisyphus.orchestrator import (
    PROMPT_FILE,
    REFACTOR_PROMPT_FILE,
    OrchestratorError,
    _Stop,
    build_orchestration_prompt,
)
from dotharness.sisyphus.paths import knowledge_file
from dotharness.sisyphus.rails import Checkpoint
from dotharness.sisyphus.stages import BUILD_STAGE, PLAN_STAGE, REFACTOR_STAGE
from tests.sisyphus.test_orchestrator import ITEM, World

EXTRA = {"refactor_set": "generic", "group_files": "a.py\nb.py", "input_file": "/x/refactor-input.json"}
TEMPLATE = (
    "Stage to run now: `<stage>`\\nStop report file: <stop report path>\\nbranch <target branch>\\n"
    "handoff <handoff dir>\\npatterns <patterns dir>\\nset <refactor_set>\\nfiles <group_files>\\ninput <input_file>\\n"
).replace("\\n", "\n")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def refactor_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Serve a temporary refactor prompt, so the test does not depend on the sibling prompt file."""
    path = tmp_path / "refactor-template.md"
    path.write_text(TEMPLATE)
    real = orchestrator.knowledge_file

    def fake(name: str) -> Path:
        return path if name == REFACTOR_PROMPT_FILE else real(name)

    monkeypatch.setattr(orchestrator, "knowledge_file", fake)
    return path


def _run(world: World) -> orchestrator._Run:
    return orchestrator._Run(ITEM, world.config(), world.options(), world.agent, world.shell, world, world.clock, world)


async def _refactor(run: orchestrator._Run, extra: dict[str, str] | None = EXTRA) -> None:
    await run.run_agent_stage(REFACTOR_STAGE, prompt_file=REFACTOR_PROMPT_FILE, extra_values=extra)


@pytest.mark.parametrize("stage", [PLAN_STAGE, BUILD_STAGE])
async def test_plan_and_build_prompts_unchanged(world: World, stage: str) -> None:
    run = _run(world)
    template = knowledge_file(PROMPT_FILE).read_text(encoding="utf-8")
    expected = build_orchestration_prompt(template, run.prompt_values(stage))
    await run.run_agent_stage(stage)
    assert world.agent_prompts == [expected]
    assert run.handoff_dir.is_dir() is (stage == BUILD_STAGE)


async def test_refactor_prompt_values_and_handoff_dir(world: World, refactor_template: Path) -> None:
    run = _run(world)
    await _refactor(run)
    sent = world.agent_prompts[0]
    for text in ("generic", "a.py", "b.py", "/x/refactor-input.json", str(run.handoff_dir)):
        assert text in sent
    for left in ("<refactor_set>", "<group_files>", "<input_file>", "<patterns dir>", "<handoff dir>"):
        assert left not in sent
    assert run.handoff_dir.is_dir()


def test_refactor_prompt_file_constant() -> None:
    assert REFACTOR_PROMPT_FILE == "sisyphus/refactor-prompt.md"


async def test_extra_values_win_and_are_not_mutated(world: World, refactor_template: Path) -> None:
    run = _run(world)
    extra = {**EXTRA, "target branch": "x"}
    before = dict(extra)
    await _refactor(run, extra)
    assert "branch x\n" in world.agent_prompts[0]
    assert extra == before
    assert run.prompt_values(REFACTOR_STAGE)["target branch"] == run.branch != "x"


async def test_patterns_dir_only_for_refactor(world: World, refactor_template: Path) -> None:
    run = _run(world)
    await _refactor(run)
    assert f"patterns {knowledge_file('patterns')}\n" in world.agent_prompts[0]
    assert "patterns dir" not in run.prompt_values(REFACTOR_STAGE)
    assert "patterns dir" not in run.prompt_values(PLAN_STAGE)
    assert "patterns dir" not in run.prompt_values(BUILD_STAGE)


async def test_patterns_dir_not_added_for_other_stages(world: World, refactor_template: Path) -> None:
    run = _run(world)
    await run.run_agent_stage(BUILD_STAGE, prompt_file=REFACTOR_PROMPT_FILE)
    assert "<patterns dir>" in world.agent_prompts[0]


async def test_none_and_empty_extra_are_identical(world: World, refactor_template: Path) -> None:
    run = _run(world)
    await _refactor(run, None)
    await _refactor(run, {})
    assert world.agent_prompts[0] == world.agent_prompts[1]
    assert "<refactor_set>" in world.agent_prompts[0]


def _stop_agent(world: World, data: dict) -> None:
    original = world.agent

    async def agent(prompt: str) -> None:
        await original(prompt)
        stop = Path(prompt.split("Stop report file: ")[1].split()[0])
        stop.write_text(json.dumps(data))

    world.agent = agent


async def test_refactor_stop_report_c5(world: World, refactor_template: Path) -> None:
    _stop_agent(world, {"checkpoint": "C5", "leaf": "generic", "detail": "d"})
    run = _run(world)
    with pytest.raises(_Stop) as info:
        await _refactor(run)
    assert info.value.checkpoint == Checkpoint.C5
    assert "generic" in str(info.value)


async def test_refactor_stop_report_bad_checkpoint(world: World, refactor_template: Path) -> None:
    _stop_agent(world, {"checkpoint": "C2", "leaf": "generic", "detail": "d"})
    run = _run(world)
    with pytest.raises(OrchestratorError):
        await _refactor(run)
