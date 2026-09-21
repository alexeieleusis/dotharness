import json

import pytest

from spec_prism_flow import generator, handoff
from spec_prism_flow.chunk import Chunk
from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)
from spec_prism_flow.errors import DecomposeError
from spec_prism_flow.generator import Leaf, Split, build_generator_prompt, run_generator
from spec_prism_flow.requirements_stage import TEMPLATE_FILENAME

TEMPLATE_TEXT = "# Requirements drafting template\n\nSome long unabridged instructions.\n"


def _chunk(path="A-1", name="auth", depth=1) -> Chunk:
    return Chunk(
        path=path,
        name=name,
        file_scope_estimate=["src/auth.py"],
        requirements_slice="the auth sub-scope text",
        depth=depth,
    )


def _make_cfg(tmp_path) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def _write_template(cfg: SpecPrismFlowConfig, text: str = TEMPLATE_TEXT) -> None:
    template_dir = cfg.harness.knowledge_dir / "spec-prism-flow"
    template_dir.mkdir(parents=True, exist_ok=True)
    (template_dir / TEMPLATE_FILENAME).write_text(text)


def test_build_generator_prompt_includes_template_and_requirements_slice():
    prompt = build_generator_prompt(_chunk(), TEMPLATE_TEXT)

    assert TEMPLATE_TEXT in prompt
    assert prompt.index(TEMPLATE_TEXT) == 0
    assert "the auth sub-scope text" in prompt
    assert "A-1" in prompt


def test_build_generator_prompt_omits_forced_instruction_by_default():
    prompt = build_generator_prompt(_chunk(), TEMPLATE_TEXT)

    assert "already judged trivial" not in prompt


def test_build_generator_prompt_appends_forced_split_instruction():
    prompt = build_generator_prompt(_chunk(), TEMPLATE_TEXT, forced_split=True)

    assert "already judged trivial" in prompt
    assert "split is required regardless" in prompt


def test_run_generator_parses_leaf_marker(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        assert stage_name == "decompose_A-1"
        return "LEAF\nThe full mini requirements doc.\nSecond line."

    monkeypatch.setattr(generator.handoff, "run_handoff", _fake_run_handoff)

    result = run_generator(_chunk(), cfg)

    assert isinstance(result, Leaf)
    assert result.doc == "The full mini requirements doc.\nSecond line."


def test_run_generator_parses_split_marker_into_child_chunks(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    children_json = json.dumps([
        {"name": "login", "file_scope_estimate": ["src/login.py"], "requirements_slice": "login slice"},
        {"name": "logout", "file_scope_estimate": ["src/logout.py"], "requirements_slice": "logout slice"},
    ])

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        return f"SPLIT\n{children_json}"

    monkeypatch.setattr(generator.handoff, "run_handoff", _fake_run_handoff)

    result = run_generator(_chunk(), cfg)

    assert isinstance(result, Split)
    assert [c.path for c in result.children] == ["A-1-1", "A-1-2"]
    assert [c.name for c in result.children] == ["login", "logout"]
    assert result.children[0].depth == 2
    assert result.children[0].file_scope_estimate == ["src/login.py"]
    assert result.children[0].requirements_slice == "login slice"


def test_run_generator_uses_forced_stage_name_when_forced_split(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    def _fake_run_handoff(prompt_text, workspace_dir, stage_name, output_filename=None):
        assert stage_name == "decompose_A-1_forced"
        assert "split is required regardless" in prompt_text
        return "LEAF\ndoc"

    monkeypatch.setattr(generator.handoff, "run_handoff", _fake_run_handoff)

    run_generator(_chunk(), cfg, forced_split=True)


def test_run_generator_raises_decompose_error_on_malformed_marker(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    monkeypatch.setattr(generator.handoff, "run_handoff", lambda *a, **k: "MAYBE\nsome text")

    with pytest.raises(DecomposeError, match=r"LEAF.*SPLIT"):
        run_generator(_chunk(), cfg)


def test_run_generator_raises_decompose_error_on_empty_output(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    monkeypatch.setattr(generator.handoff, "run_handoff", lambda *a, **k: "")

    with pytest.raises(DecomposeError, match="empty"):
        run_generator(_chunk(), cfg)


def test_run_generator_raises_decompose_error_on_invalid_split_json(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    monkeypatch.setattr(generator.handoff, "run_handoff", lambda *a, **k: "SPLIT\nnot json")

    with pytest.raises(DecomposeError, match="not valid JSON"):
        run_generator(_chunk(), cfg)


def test_run_generator_raises_decompose_error_on_too_few_split_children(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    children_json = json.dumps([{"name": "only", "file_scope_estimate": [], "requirements_slice": "x"}])
    monkeypatch.setattr(generator.handoff, "run_handoff", lambda *a, **k: f"SPLIT\n{children_json}")

    with pytest.raises(DecomposeError, match="2-4"):
        run_generator(_chunk(), cfg)


def test_run_generator_raises_decompose_error_on_too_many_split_children(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    children_json = json.dumps([
        {"name": f"c{i}", "file_scope_estimate": [], "requirements_slice": "x"} for i in range(5)
    ])
    monkeypatch.setattr(generator.handoff, "run_handoff", lambda *a, **k: f"SPLIT\n{children_json}")

    with pytest.raises(DecomposeError, match="2-4"):
        run_generator(_chunk(), cfg)


def test_run_generator_raises_decompose_error_on_split_child_missing_keys(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    children_json = json.dumps([
        {"name": "only-name"},
        {"name": "second", "file_scope_estimate": [], "requirements_slice": "x"},
    ])
    monkeypatch.setattr(generator.handoff, "run_handoff", lambda *a, **k: f"SPLIT\n{children_json}")

    with pytest.raises(DecomposeError, match="missing required keys"):
        run_generator(_chunk(), cfg)


def test_run_generator_raises_decompose_error_when_template_missing(tmp_path):
    cfg = _make_cfg(tmp_path)

    with pytest.raises(DecomposeError, match="template not found"):
        run_generator(_chunk(), cfg)


def test_run_generator_translates_handoff_error_cleanly(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _write_template(cfg)

    def _raise_handoff_error(*a, **k):
        raise handoff.HandoffError("Expected output file not found: /nowhere")  # noqa: TRY003

    monkeypatch.setattr(generator.handoff, "run_handoff", _raise_handoff_error)

    with pytest.raises(DecomposeError):
        run_generator(_chunk(), cfg)
