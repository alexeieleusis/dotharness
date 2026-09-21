from pathlib import Path

from spec_prism_flow.graph import Graph, load_graph, validate_graph, write_graph
from spec_prism_flow.phase_file import PhaseFile


def _phase(number: int, name: str, scope: list[str], depends_on: str) -> PhaseFile:
    return PhaseFile(
        number=number,
        name=name,
        scope=scope,
        requirements="req",
        acceptance_criteria=["ok"],
        manual_test_checklist=["check"],
        depends_on=depends_on,
    )


def test_write_and_load_graph_round_trips(tmp_path):
    graph = Graph(
        nodes=["01-a-leaf", "02-b-leaf"],
        edges=[("02-b-leaf", "01-a-leaf")],
    )
    p = tmp_path / "graph.json"

    write_graph(graph, p)
    loaded = load_graph(p)

    assert loaded == graph


def test_write_graph_edge_order(tmp_path):
    graph = Graph(
        nodes=["01-a-leaf", "02-b-leaf", "03-c-leaf"],
        edges=[("03-c-leaf", "02-b-leaf"), ("02-b-leaf", "01-a-leaf")],
    )
    p = tmp_path / "graph.json"

    write_graph(graph, p)
    data = p.read_text()

    assert '"edges"' in data
    loaded = load_graph(p)
    assert loaded.edges == [("03-c-leaf", "02-b-leaf"), ("02-b-leaf", "01-a-leaf")]


def test_valid_graph_returns_no_violations():
    phase_files = [
        _phase(1, "a", ["a.py"], "None (first phase)."),
        _phase(2, "b", ["b.py"], "Phase 1 merged."),
    ]
    graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[("02-b-leaf", "01-a-leaf")])

    assert validate_graph(graph, phase_files) == []


def test_orphaned_node_detected():
    phase_files = [_phase(1, "a", ["a.py"], "None (first phase).")]
    graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[])

    violations = validate_graph(graph, phase_files)

    assert any("02-b-leaf" in v for v in violations)


def test_orphaned_phase_file_detected():
    phase_files = [
        _phase(1, "a", ["a.py"], "None (first phase)."),
        _phase(2, "b", ["b.py"], "Phase 1 merged."),
    ]
    graph = Graph(nodes=["01-a-leaf"], edges=[])

    violations = validate_graph(graph, phase_files)

    assert any("02-b-leaf" in v for v in violations)


def test_cycle_detected():
    phase_files = [
        _phase(1, "a", ["a.py"], "Phase 2 merged."),
        _phase(2, "b", ["b.py"], "Phase 1 merged."),
    ]
    graph = Graph(
        nodes=["01-a-leaf", "02-b-leaf"],
        edges=[("01-a-leaf", "02-b-leaf"), ("02-b-leaf", "01-a-leaf")],
    )

    violations = validate_graph(graph, phase_files)

    assert any("cycle" in v.lower() for v in violations)


def test_disjoint_scope_overlap_detected():
    # Two unrelated leaves (no path between them) sharing a scope file.
    phase_files = [
        _phase(1, "a", ["shared.py"], "None (first phase)."),
        _phase(2, "b", ["shared.py"], "None (independent feature)."),
    ]
    graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[])

    violations = validate_graph(graph, phase_files)

    assert any("shared.py" in v for v in violations)


def test_disjoint_scope_no_violation_when_path_exists():
    phase_files = [
        _phase(1, "a", ["shared.py"], "None (first phase)."),
        _phase(2, "b", ["shared.py"], "Phase 1 merged."),
    ]
    graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[("02-b-leaf", "01-a-leaf")])

    assert validate_graph(graph, phase_files) == []


def test_disjoint_scope_no_violation_when_transitive_path_exists():
    # Phase 3 depends on phase 2, which depends on phase 1: no direct edge between
    # 01 and 03, but they are connected through the multi-hop BFS closure.
    phase_files = [
        _phase(1, "a", ["shared.py"], "None (first phase)."),
        _phase(2, "b", ["b.py"], "Phase 1 merged."),
        _phase(3, "c", ["shared.py"], "Phase 2 merged."),
    ]
    graph = Graph(
        nodes=["01-a-leaf", "02-b-leaf", "03-c-leaf"],
        edges=[("03-c-leaf", "02-b-leaf"), ("02-b-leaf", "01-a-leaf")],
    )

    assert validate_graph(graph, phase_files) == []


def test_missing_linear_chain_edge_detected():
    phase_files = [
        _phase(1, "a", ["a.py"], "None (first phase)."),
        _phase(2, "b", ["b.py"], "Phase 1 merged."),
    ]
    graph = Graph(nodes=["01-a-leaf", "02-b-leaf"], edges=[])

    violations = validate_graph(graph, phase_files)

    assert any("02-b-leaf" in v and "01-a-leaf" in v for v in violations)


def test_linear_chain_check_does_not_assume_numeric_predecessor():
    # Phase 8 starts an independent feature that depends on phase 1, not phase 7.
    phase_files = [
        _phase(1, "a", ["a.py"], "None (first phase)."),
        _phase(7, "g", ["g.py"], "Phase 1 merged."),
        _phase(8, "h", ["h.py"], "Phase 1 merged."),
    ]
    graph = Graph(
        nodes=["01-a-leaf", "07-g-leaf", "08-h-leaf"],
        edges=[("07-g-leaf", "01-a-leaf"), ("08-h-leaf", "01-a-leaf")],
    )

    violations = validate_graph(graph, phase_files)

    assert violations == []


def test_real_graph_and_phase_files_are_valid():
    from spec_prism_flow.phase_file import parse_phase_file

    phases_dir = Path(__file__).parent.parent / "docs" / "phases"
    graph = load_graph(phases_dir / "graph.json")
    phase_files = [parse_phase_file(p) for p in sorted(phases_dir.glob("*-leaf.md"))]

    assert validate_graph(graph, phase_files) == []
