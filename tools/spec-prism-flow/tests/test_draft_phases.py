import pytest
from click.testing import CliRunner

from spec_prism_flow.chunk import Chunk, ChunkNode, write_tree
from spec_prism_flow.cli import cli
from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)
from spec_prism_flow.decompose import TREE_FILENAME
from spec_prism_flow.draft_phases import DraftPhasesError, run_draft_phases, target_phase_paths
from spec_prism_flow.phase_file import parse_phase_file


def _make_cfg(tmp_path) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def _chunk(path, name, file_scope_estimate, depth) -> Chunk:
    return Chunk(
        path=path,
        name=name,
        file_scope_estimate=file_scope_estimate,
        requirements_slice=f"slice for {path}",
        depth=depth,
    )


def _goals_doc(*goal_texts: str) -> str:
    # Uses "###", never "##": a "## " line inside a mini-doc would collide with
    # phase_file.py's fixed five-header scan once this text is embedded verbatim as a
    # phase file's Requirements body (the real corpus's own phase files never do this either).
    body = "\n".join(f"- **G{i + 1}.** {text}" for i, text in enumerate(goal_texts))
    return f"### Goals\n{body}\n\n### Non-goals\n- Not this.\n"


def _write_tree(cfg, tree: ChunkNode) -> None:
    cfg.plan.workspace_dir.mkdir(parents=True, exist_ok=True)
    write_tree(tree, cfg.plan.workspace_dir / TREE_FILENAME)


# --- basic drafting -----------------------------------------------------------------------------


def test_writes_one_phase_file_per_leaf_in_dfs_order_and_skips_internal_nodes(tmp_path):
    cfg = _make_cfg(tmp_path)
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["a.py"], 1), leaf_doc=_goals_doc("Do the first thing."))
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["b.py"], 1), leaf_doc=_goals_doc("Do the second thing."))
    branch = ChunkNode(chunk=_chunk("A-1-parent", "branch", [], 1), children=[leaf_1])
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[branch, leaf_2])
    _write_tree(cfg, root)

    result = run_draft_phases(cfg)

    assert result.leaves_drafted == 2
    assert result.internal_nodes_skipped == 2  # root + branch
    written_names = sorted(p.name for p in cfg.plan.phase_dir.glob("*-leaf.md"))
    assert written_names == ["01-first-leaf.md", "02-second-leaf.md"]
    assert len(result.written) == 2


def test_requirements_verbatim_scope_and_depends_on_chain(tmp_path):
    cfg = _make_cfg(tmp_path)
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["a.py", "b.py"], 1), leaf_doc=_goals_doc("First goal."))
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["c.py"], 1), leaf_doc=_goals_doc("Second goal."))
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2])
    _write_tree(cfg, root)

    run_draft_phases(cfg)

    phase_1 = parse_phase_file(cfg.plan.phase_dir / "01-first-leaf.md")
    phase_2 = parse_phase_file(cfg.plan.phase_dir / "02-second-leaf.md")
    assert "### Goals" in phase_1.requirements
    assert "First goal." in phase_1.requirements
    assert phase_1.scope == ["a.py", "b.py"]
    assert phase_1.depends_on == "None (first phase)."
    assert phase_2.depends_on == "Phase 1 merged."


def test_rerun_against_unchanged_tree_is_byte_identical(tmp_path):
    cfg = _make_cfg(tmp_path)
    leaf = ChunkNode(chunk=_chunk("A-1", "only", ["a.py"], 1), leaf_doc=_goals_doc("Only goal."))
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    _write_tree(cfg, root)

    run_draft_phases(cfg)
    first_bytes = (cfg.plan.phase_dir / "01-only-leaf.md").read_bytes()

    run_draft_phases(cfg)
    second_bytes = (cfg.plan.phase_dir / "01-only-leaf.md").read_bytes()

    assert first_bytes == second_bytes


def test_escalated_node_raises_draft_phases_error(tmp_path):
    cfg = _make_cfg(tmp_path)
    escalated = ChunkNode(chunk=_chunk("A-1", "stuck", ["a.py"], 1), escalation_reason="depth cap reached")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[escalated])
    _write_tree(cfg, root)

    with pytest.raises(DraftPhasesError, match="escalation_reason"):
        run_draft_phases(cfg)


def test_missing_tree_file_raises_draft_phases_error(tmp_path):
    cfg = _make_cfg(tmp_path)

    with pytest.raises(DraftPhasesError, match="plan decompose"):
        run_draft_phases(cfg)


# --- target_phase_paths ---------------------------------------------------------------------------


def test_target_phase_paths_matches_what_run_draft_phases_writes(tmp_path):
    cfg = _make_cfg(tmp_path)
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["a.py"], 1), leaf_doc=_goals_doc("First goal."))
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["b.py"], 1), leaf_doc=_goals_doc("Second goal."))
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2])
    _write_tree(cfg, root)

    paths_before_write = target_phase_paths(cfg)
    result = run_draft_phases(cfg)

    assert paths_before_write == result.written


# --- sizing outliers -----------------------------------------------------------------------------


def _in_band_scope(prefix: str) -> list[str]:
    return [f"{prefix}{i}.py" for i in range(6)]


def _in_band_words(prefix: str = "word") -> str:
    return " ".join([prefix] * 600)


def test_sizing_outliers_are_flagged_but_do_not_block_write(tmp_path):
    cfg = _make_cfg(tmp_path)
    oversized_doc = _goals_doc("A goal.") + "\n" + _in_band_words()
    small_scope_leaf = ChunkNode(chunk=_chunk("A-1", "small", ["only.py"], 1), leaf_doc=oversized_doc)
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[small_scope_leaf])
    _write_tree(cfg, root)

    result = run_draft_phases(cfg)

    assert (cfg.plan.phase_dir / "01-small-leaf.md").exists()
    assert any("01-small-leaf.md" in outlier for outlier in result.outliers)


def test_in_band_leaf_produces_no_outliers(tmp_path):
    cfg = _make_cfg(tmp_path)
    doc = _goals_doc("A goal.") + "\n" + _in_band_words()
    leaf = ChunkNode(chunk=_chunk("A-1", "fine", _in_band_scope("f"), 1), leaf_doc=doc)
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    _write_tree(cfg, root)

    result = run_draft_phases(cfg)

    assert result.outliers == []


# --- acceptance criteria / manual test checklist derivation -------------------------------------


def test_acceptance_criteria_derived_from_goals_section(tmp_path):
    cfg = _make_cfg(tmp_path)
    doc = _goals_doc("Support widgets.", "Reject invalid input.")
    leaf = ChunkNode(chunk=_chunk("A-1", "widgets", ["a.py"], 1), leaf_doc=doc)
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    _write_tree(cfg, root)

    run_draft_phases(cfg)

    phase = parse_phase_file(cfg.plan.phase_dir / "01-widgets-leaf.md")
    assert phase.acceptance_criteria == ["Support widgets.", "Reject invalid input."]
    assert phase.manual_test_checklist == [
        "Manually verify: Support widgets.",
        "Manually verify: Reject invalid input.",
    ]


def test_acceptance_criteria_falls_back_to_bare_goal_markers_without_heading(tmp_path):
    cfg = _make_cfg(tmp_path)
    doc = "Some prose.\n\n- **G1.** Do the thing.\n- Not a goal, just a bullet.\n"
    leaf = ChunkNode(chunk=_chunk("A-1", "flat", ["a.py"], 1), leaf_doc=doc)
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    _write_tree(cfg, root)

    run_draft_phases(cfg)

    phase = parse_phase_file(cfg.plan.phase_dir / "01-flat-leaf.md")
    assert phase.acceptance_criteria == ["Do the thing."]


def test_acceptance_criteria_does_not_treat_non_goals_heading_as_goals(tmp_path):
    cfg = _make_cfg(tmp_path)
    doc = "### Non-Goals\n- Not this.\n"
    leaf = ChunkNode(chunk=_chunk("A-1", "leaf", ["a.py"], 1), leaf_doc=doc)
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    _write_tree(cfg, root)

    run_draft_phases(cfg)

    phase = parse_phase_file(cfg.plan.phase_dir / "01-leaf-leaf.md")
    assert len(phase.acceptance_criteria) == 1
    assert "No structured, testable requirements" in phase.acceptance_criteria[0]


def test_acceptance_criteria_placeholder_when_nothing_recognizable(tmp_path):
    cfg = _make_cfg(tmp_path)
    leaf = ChunkNode(chunk=_chunk("A-1", "plain", ["a.py"], 1), leaf_doc="Just an unstructured mini-doc.")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    _write_tree(cfg, root)

    run_draft_phases(cfg)

    phase = parse_phase_file(cfg.plan.phase_dir / "01-plain-leaf.md")
    assert len(phase.acceptance_criteria) == 1
    assert "No structured, testable requirements" in phase.acceptance_criteria[0]
    assert phase.manual_test_checklist == phase.acceptance_criteria


# --- CLI overwrite guard ---------------------------------------------------------------------------

MINIMAL_TOML = """
[plan]
workspace_dir = "workspace"
phase_dir = "phases"
"""


def _write_cli_tree(tmp_path) -> None:
    leaf = ChunkNode(chunk=_chunk("A-1", "only", ["a.py"], 1), leaf_doc=_goals_doc("Only goal."))
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf])
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    write_tree(root, tmp_path / "workspace" / TREE_FILENAME)


def test_cli_draft_phases_prompts_before_overwriting_existing_phase_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    _write_cli_tree(tmp_path)
    phase_dir = tmp_path / "phases"
    phase_dir.mkdir()
    (phase_dir / "01-only-leaf.md").write_text("hand-edited content")

    result = CliRunner().invoke(cli, ["plan", "draft-phases"], input="n\n")

    assert result.exit_code != 0
    assert (phase_dir / "01-only-leaf.md").read_text() == "hand-edited content"


def test_cli_draft_phases_yes_skips_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    _write_cli_tree(tmp_path)
    phase_dir = tmp_path / "phases"
    phase_dir.mkdir()
    (phase_dir / "01-only-leaf.md").write_text("hand-edited content")

    result = CliRunner().invoke(cli, ["plan", "draft-phases", "--yes"])

    assert result.exit_code == 0, result.output
    assert (phase_dir / "01-only-leaf.md").read_text() != "hand-edited content"


def test_cli_draft_phases_no_prompt_when_nothing_exists_yet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".spec-prism-flow.toml").write_text(MINIMAL_TOML)
    _write_cli_tree(tmp_path)

    result = CliRunner().invoke(cli, ["plan", "draft-phases"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "phases" / "01-only-leaf.md").exists()
