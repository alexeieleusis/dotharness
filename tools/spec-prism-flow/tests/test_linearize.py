import pytest

from spec_prism_flow.chunk import Chunk, ChunkNode
from spec_prism_flow.errors import DecomposeError
from spec_prism_flow.linearize import linearize


def _chunk(path, name, file_scope_estimate, depth) -> Chunk:
    return Chunk(
        path=path,
        name=name,
        file_scope_estimate=file_scope_estimate,
        requirements_slice=f"requirements slice for {path}",
        depth=depth,
    )


def test_linearize_single_leaf_tree():
    root = ChunkNode(chunk=_chunk("A", "root", ["src/a.py"], 0), leaf_doc="the only doc")

    result = linearize(root)

    assert len(result.leaves) == 1
    assert result.leaves[0].number == 1
    assert result.leaves[0].depends_on_number is None
    assert result.graph.nodes == ["01-root-leaf"]
    assert result.graph.edges == []


def test_linearize_dfs_order_and_depends_on_chain():
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["src/one.py"], 1), leaf_doc="first doc, standalone")
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["src/two.py"], 1), leaf_doc="second doc, standalone")
    leaf_3 = ChunkNode(chunk=_chunk("A-3", "third", ["src/three.py"], 1), leaf_doc="third doc, standalone")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2, leaf_3])

    result = linearize(root)

    numbers_and_paths = [(lv.number, lv.chunk.path) for lv in result.leaves]
    assert numbers_and_paths == [(1, "A-1"), (2, "A-2"), (3, "A-3")]
    assert [lv.depends_on_number for lv in result.leaves] == [None, 1, 2]


def test_linearize_asymmetric_depth_dfs_order():
    deep_leaf = ChunkNode(chunk=_chunk("A-1-1", "deep", ["src/deep.py"], 2), leaf_doc="deep doc")
    branch_a = ChunkNode(chunk=_chunk("A-1", "branch-a", [], 1), children=[deep_leaf])
    branch_b = ChunkNode(chunk=_chunk("A-2", "branch-b", ["src/b.py"], 1), leaf_doc="branch b doc")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[branch_a, branch_b])

    result = linearize(root)

    assert [lv.chunk.path for lv in result.leaves] == ["A-1-1", "A-2"]


def test_linearize_no_edge_added_for_unrelated_leaves_with_no_reference_or_shared_scope():
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["src/one.py"], 1), leaf_doc="first doc, standalone")
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["src/two.py"], 1), leaf_doc="second doc, standalone")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2])

    result = linearize(root)

    assert result.graph.edges == []


def test_linearize_adds_edge_when_mini_doc_references_sibling_chunk_path():
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["src/one.py"], 1), leaf_doc="first doc")
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["src/two.py"], 1), leaf_doc="second doc")
    leaf_3 = ChunkNode(
        chunk=_chunk("A-3", "third", ["src/three.py"], 1),
        leaf_doc="third doc that builds on chunk A-1's setup",
    )
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2, leaf_3])

    result = linearize(root)

    assert result.graph.edges == [("03-third-leaf", "01-first-leaf")]


def test_linearize_adds_edge_when_mini_doc_references_sibling_chunk_name():
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "auth-flow", ["src/one.py"], 1), leaf_doc="first doc")
    leaf_2 = ChunkNode(
        chunk=_chunk("A-2", "second", ["src/two.py"], 1),
        leaf_doc="second doc that depends on the auth-flow chunk's output",
    )
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2])

    result = linearize(root)

    assert ("02-second-leaf", "01-auth-flow-leaf") in result.graph.edges


def test_linearize_adds_edge_when_leaves_share_file_scope_with_no_textual_reference():
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["src/shared.py"], 1), leaf_doc="first doc, standalone")
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["src/two.py"], 1), leaf_doc="second doc, standalone")
    leaf_3 = ChunkNode(chunk=_chunk("A-3", "third", ["src/shared.py"], 1), leaf_doc="third doc, standalone")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2, leaf_3])

    result = linearize(root)

    assert ("03-third-leaf", "01-first-leaf") in result.graph.edges
    assert ("02-second-leaf", "01-first-leaf") not in result.graph.edges


def test_linearize_does_not_raise_when_shared_scope_leaves_are_explicitly_linked():
    leaf_1 = ChunkNode(chunk=_chunk("A-1", "first", ["src/shared.py"], 1), leaf_doc="first doc")
    leaf_2 = ChunkNode(chunk=_chunk("A-2", "second", ["src/two.py"], 1), leaf_doc="second doc")
    leaf_3 = ChunkNode(
        chunk=_chunk("A-3", "third", ["src/shared.py"], 1),
        leaf_doc="third doc that reuses chunk A-1's setup",
    )
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[leaf_1, leaf_2, leaf_3])

    result = linearize(root)

    assert ("03-third-leaf", "01-first-leaf") in result.graph.edges


def test_linearize_raises_decompose_error_when_leaf_count_exceeds_two_digit_naming_limit():
    leaves = [
        ChunkNode(chunk=_chunk(f"A-{i}", f"leaf-{i}", [f"src/{i}.py"], 1), leaf_doc=f"doc {i}") for i in range(100)
    ]
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=leaves)

    with pytest.raises(DecomposeError, match="100 leaves"):
        linearize(root)


def test_linearize_raises_decompose_error_when_escalated_node_remains():
    escalated = ChunkNode(chunk=_chunk("A-1", "stuck", ["src/a.py"], 1), escalation_reason="depth cap reached")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[escalated])

    with pytest.raises(DecomposeError, match="flagged for human review"):
        linearize(root)


def test_linearize_raises_decompose_error_when_escalated_node_is_nested():
    escalated = ChunkNode(chunk=_chunk("A-1-1", "stuck", ["src/a.py"], 2), escalation_reason="trivial deadlock")
    branch = ChunkNode(chunk=_chunk("A-1", "branch", [], 1), children=[escalated])
    leaf = ChunkNode(chunk=_chunk("A-2", "ok", ["src/b.py"], 1), leaf_doc="fine doc")
    root = ChunkNode(chunk=_chunk("A", "root", [], 0), children=[branch, leaf])

    with pytest.raises(DecomposeError, match="A-1-1"):
        linearize(root)
