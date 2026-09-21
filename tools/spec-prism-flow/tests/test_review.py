import pytest

from spec_prism_flow.config import (
    AgentConfig,
    BuildConfig,
    HarnessSection,
    PlanConfig,
    ReviewConfig,
    SpecPrismFlowConfig,
    VibeHealConfig,
)
from spec_prism_flow.decompose import GRAPH_FILENAME
from spec_prism_flow.graph import Graph, write_graph
from spec_prism_flow.overview_stage import OPEN_QUESTIONS_FILENAME
from spec_prism_flow.phase_file import PhaseFile, phase_file_name, render_phase_file
from spec_prism_flow.requirements_stage import REQUIREMENTS_FILENAME
from spec_prism_flow.review import REVIEW_LOG_FILENAME, ReviewError, run_review

REQUIREMENTS_TEXT = """# Title

## 1. Goals

Some goals text.

## 2. Non-goals

Some non-goals text, never quoted anywhere downstream.
"""


def _make_cfg(tmp_path) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def _phase(number, name, scope, requirements, depends_on) -> PhaseFile:
    return PhaseFile(
        number=number,
        name=name,
        scope=scope,
        requirements=requirements,
        acceptance_criteria=["ok"],
        manual_test_checklist=["check"],
        depends_on=depends_on,
    )


def _write_phase(phase_dir, phase: PhaseFile) -> None:
    phase_dir.mkdir(parents=True, exist_ok=True)
    (phase_dir / phase_file_name(phase.number, phase.name)).write_text(render_phase_file(phase))


def _write_corpus(cfg, phases, graph: Graph, requirements_text=REQUIREMENTS_TEXT, open_questions_text=None) -> None:
    cfg.plan.workspace_dir.mkdir(parents=True, exist_ok=True)
    cfg.plan.phase_dir.mkdir(parents=True, exist_ok=True)
    for phase in phases:
        _write_phase(cfg.plan.phase_dir, phase)
    write_graph(graph, cfg.plan.phase_dir / GRAPH_FILENAME)
    (cfg.plan.workspace_dir / REQUIREMENTS_FILENAME).write_text(requirements_text)
    if open_questions_text is not None:
        (cfg.plan.workspace_dir / OPEN_QUESTIONS_FILENAME).write_text(open_questions_text)


def _well_formed_phases() -> list[PhaseFile]:
    return [
        _phase(1, "a", ["a.py"], "Cites §1 here, verbatim.", "None (first phase)."),
        _phase(2, "b", ["b.py"], "Second phase, cites nothing further.", "Phase 1 merged."),
    ]


def _well_formed_graph() -> Graph:
    return Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[("02-b-leaf", "01-a-leaf")])


# --- happy path -----------------------------------------------------------------------------


def test_well_formed_corpus_passes_dependency_and_graph_checks(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_corpus(cfg, _well_formed_phases(), _well_formed_graph())

    report = run_review(cfg)

    dependency_check = next(c for c in report.checks if c.name == "Dependency order")
    graph_check = next(c for c in report.checks if c.name == "Graph agreement")
    stale_check = next(c for c in report.checks if c.name == "Stale open questions")
    assert dependency_check.passed
    assert graph_check.passed
    assert stale_check.passed  # no OPEN_QUESTIONS.md at all is not stale


def test_section_coverage_flags_only_the_uncovered_section(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_corpus(cfg, _well_formed_phases(), _well_formed_graph())

    report = run_review(cfg)

    coverage_check = next(c for c in report.checks if c.name == "Section coverage")
    assert not coverage_check.passed
    assert any("§2" in v for v in coverage_check.violations)
    assert not any("§1" in v for v in coverage_check.violations)


# --- each check, in isolation -----------------------------------------------------------------


def test_dependency_order_violation_detected(tmp_path):
    cfg = _make_cfg(tmp_path)
    phases = [
        _phase(1, "a", ["a.py"], "Cites §1 and §2.", "None (first phase)."),
        _phase(2, "b", ["b.py"], "Cites §1 and §2.", "Phase 5 merged."),
    ]
    _write_corpus(cfg, phases, Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[]))

    report = run_review(cfg)

    check = next(c for c in report.checks if c.name == "Dependency order")
    assert not check.passed
    assert any("Phase 2" in v for v in check.violations)


def test_graph_agreement_violation_detected(tmp_path):
    cfg = _make_cfg(tmp_path)
    phases = _well_formed_phases()
    broken_graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[])  # missing the linear-chain edge

    _write_corpus(cfg, phases, broken_graph)

    report = run_review(cfg)

    check = next(c for c in report.checks if c.name == "Graph agreement")
    assert not check.passed
    assert any("01-a-leaf" in v and "02-b-leaf" in v for v in check.violations)


def test_stale_open_question_detected(tmp_path):
    cfg = _make_cfg(tmp_path)
    open_questions = "1. **Resolved:** already answered.\n2. Still an open, unanswered question?\n"
    _write_corpus(cfg, _well_formed_phases(), _well_formed_graph(), open_questions_text=open_questions)

    report = run_review(cfg)

    check = next(c for c in report.checks if c.name == "Stale open questions")
    assert not check.passed
    assert any("Still an open" in v for v in check.violations)
    assert not any("already answered" in v for v in check.violations)


def test_deferred_open_question_is_not_stale(tmp_path):
    cfg = _make_cfg(tmp_path)
    open_questions = "1. **Deferred:** intentionally punted to a later phase.\n"
    _write_corpus(cfg, _well_formed_phases(), _well_formed_graph(), open_questions_text=open_questions)

    report = run_review(cfg)

    check = next(c for c in report.checks if c.name == "Stale open questions")
    assert check.passed


# --- all four run and report independently in one pass --------------------------------------


def test_all_four_broken_conditions_reported_in_a_single_run(tmp_path):
    cfg = _make_cfg(tmp_path)
    phases = [
        _phase(1, "a", ["a.py"], "Cites §1 only.", "None (first phase)."),
        # Broken depends_on (should name phase 1, names none) — a dependency-order
        # violation that does NOT also trip the graph's own linear-chain-coverage check,
        # since that check only fires when the text actually names the predecessor.
        _phase(2, "b", ["b.py"], "Cites nothing new.", "None (first phase)."),
    ]
    # An orphan node ("99-ghost-leaf" has no matching phase file) — a graph-agreement
    # violation independent of the dependency-order text above.
    broken_graph = Graph(nodes=["01-a-leaf", "02-b-leaf", "99-ghost-leaf"], edges=[])
    open_questions = "1. Unanswered, undeferred question.\n"

    _write_corpus(cfg, phases, broken_graph, open_questions_text=open_questions)

    report = run_review(cfg)

    by_name = {c.name: c for c in report.checks}
    assert not by_name["Dependency order"].passed
    assert not by_name["Graph agreement"].passed
    assert not by_name["Section coverage"].passed  # §2 never cited
    assert not by_name["Stale open questions"].passed
    assert not report.all_passed


# --- side effects -----------------------------------------------------------------------------


def test_review_never_modifies_inspected_files(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_corpus(cfg, _well_formed_phases(), _well_formed_graph())
    phase_path = cfg.plan.phase_dir / "01-a-leaf.md"
    graph_path = cfg.plan.phase_dir / GRAPH_FILENAME
    requirements_path = cfg.plan.workspace_dir / REQUIREMENTS_FILENAME
    before = {
        phase_path: phase_path.read_bytes(),
        graph_path: graph_path.read_bytes(),
        requirements_path: requirements_path.read_bytes(),
    }

    run_review(cfg)

    for path, content in before.items():
        assert path.read_bytes() == content


def test_review_writes_report_to_console_and_log_file(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_corpus(cfg, _well_formed_phases(), _well_formed_graph())

    report = run_review(cfg)

    log_path = cfg.plan.workspace_dir / REVIEW_LOG_FILENAME
    assert log_path.exists()
    log_text = log_path.read_text()
    for check in report.checks:
        assert check.name in log_text


# --- missing inputs ------------------------------------------------------------------------


def test_missing_phase_files_raises_review_error(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.plan.phase_dir.mkdir(parents=True)
    cfg.plan.workspace_dir.mkdir(parents=True)

    with pytest.raises(ReviewError, match="plan draft-phases"):
        run_review(cfg)


def test_missing_graph_raises_review_error(tmp_path):
    cfg = _make_cfg(tmp_path)
    _write_phase(cfg.plan.phase_dir, _well_formed_phases()[0])

    with pytest.raises(ReviewError, match="plan decompose"):
        run_review(cfg)


def test_missing_requirements_raises_review_error(tmp_path):
    cfg = _make_cfg(tmp_path)
    phases = _well_formed_phases()
    for phase in phases:
        _write_phase(cfg.plan.phase_dir, phase)
    write_graph(_well_formed_graph(), cfg.plan.phase_dir / GRAPH_FILENAME)

    with pytest.raises(ReviewError, match="plan draft-requirements"):
        run_review(cfg)
