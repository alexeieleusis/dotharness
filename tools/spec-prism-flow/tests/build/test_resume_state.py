from spec_prism_flow.build import resume_state
from spec_prism_flow.build.resume_state import ResumeState
from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)


def _make_cfg(tmp_path) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(state_dir=tmp_path / "state"),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def _state(**overrides) -> ResumeState:
    defaults = {
        "repo": "alexeieleusis/dotharness",
        "branch": "phase-08-x",
        "pr_number": 42,
        "pr_url": "https://github.com/alexeieleusis/dotharness/pull/42",
        "cycle_index": 0,
    }
    defaults.update(overrides)
    return ResumeState(**defaults)


def test_resume_state_path_is_keyed_by_repo_slug_and_branch(tmp_path):
    cfg = _make_cfg(tmp_path)

    path = resume_state.resume_state_path(cfg, "alexeieleusis/dotharness", "phase-08-x")

    assert path == tmp_path / "state" / "alexeieleusis-dotharness" / "phase-08-x" / "resume_state.json"


def test_resume_state_path_different_repos_or_branches_never_collide(tmp_path):
    cfg = _make_cfg(tmp_path)

    path_a = resume_state.resume_state_path(cfg, "alexeieleusis/dotharness", "phase-08-x")
    path_b = resume_state.resume_state_path(cfg, "alexeieleusis/other-repo", "phase-08-x")
    path_c = resume_state.resume_state_path(cfg, "alexeieleusis/dotharness", "phase-09-y")

    assert len({path_a, path_b, path_c}) == 3


def test_load_resume_state_returns_none_when_file_absent(tmp_path):
    path = tmp_path / "nonexistent" / "resume_state.json"

    assert resume_state.load_resume_state(path) is None


def test_save_then_load_round_trips_resume_state_including_cycle_index(tmp_path):
    path = tmp_path / "state" / "resume_state.json"
    state = _state(cycle_index=2)

    resume_state.save_resume_state(path, state)
    loaded = resume_state.load_resume_state(path)

    assert loaded == state


def test_save_resume_state_creates_parent_directories(tmp_path):
    path = tmp_path / "does" / "not" / "exist" / "resume_state.json"

    resume_state.save_resume_state(path, _state())

    assert path.exists()
    assert resume_state.load_resume_state(path) == _state()


def test_clear_resume_state_removes_existing_file(tmp_path):
    path = tmp_path / "resume_state.json"
    resume_state.save_resume_state(path, _state())

    resume_state.clear_resume_state(path)

    assert not path.exists()
    assert resume_state.load_resume_state(path) is None


def test_clear_resume_state_is_a_no_op_when_file_absent(tmp_path):
    path = tmp_path / "nonexistent" / "resume_state.json"

    resume_state.clear_resume_state(path)

    assert not path.exists()
