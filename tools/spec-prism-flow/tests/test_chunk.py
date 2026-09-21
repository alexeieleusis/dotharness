import json

import pytest

from spec_prism_flow.chunk import (
    Chunk,
    ChunkError,
    ChunkNode,
    DecomposeError,
    load_tree,
    node_from_dict,
    node_to_dict,
    write_tree,
)


def _chunk(path="A", name="root", file_scope_estimate=None, depth=0) -> Chunk:
    return Chunk(
        path=path,
        name=name,
        file_scope_estimate=file_scope_estimate if file_scope_estimate is not None else ["src/a.py"],
        requirements_slice="some requirements text",
        depth=depth,
    )


def test_chunk_node_rejects_both_leaf_doc_and_children():
    with pytest.raises(ChunkError, match="exactly one"):
        ChunkNode(chunk=_chunk(), leaf_doc="doc", children=[ChunkNode(chunk=_chunk("A-1"), leaf_doc="child doc")])


def test_chunk_node_rejects_neither_leaf_doc_nor_children():
    with pytest.raises(ChunkError, match="exactly one"):
        ChunkNode(chunk=_chunk())


def test_chunk_node_rejects_all_three_variants_set():
    with pytest.raises(ChunkError, match="exactly one"):
        ChunkNode(chunk=_chunk(), leaf_doc="doc", escalation_reason="flagged")


def test_chunk_node_rejects_empty_children_list():
    with pytest.raises(ChunkError, match="empty children"):
        ChunkNode(chunk=_chunk(), children=[])


def test_chunk_node_leaf_variant_is_leaf_only():
    node = ChunkNode(chunk=_chunk(), leaf_doc="the doc")
    assert node.is_leaf
    assert not node.is_escalated


def test_chunk_node_children_variant_is_neither_leaf_nor_escalated():
    child = ChunkNode(chunk=_chunk("A-1", depth=1), leaf_doc="child doc")
    node = ChunkNode(chunk=_chunk(), children=[child])
    assert not node.is_leaf
    assert not node.is_escalated


def test_chunk_node_escalation_variant_is_escalated_only():
    node = ChunkNode(chunk=_chunk(), escalation_reason="depth cap reached")
    assert node.is_escalated
    assert not node.is_leaf


def test_node_to_dict_and_from_dict_round_trip_leaf():
    node = ChunkNode(chunk=_chunk(), leaf_doc="the doc")
    restored = node_from_dict(node_to_dict(node))
    assert restored == node


def test_node_to_dict_and_from_dict_round_trip_split_tree():
    child_a = ChunkNode(chunk=_chunk("A-1", "first", depth=1), leaf_doc="first doc")
    child_b = ChunkNode(chunk=_chunk("A-2", "second", depth=1), leaf_doc="second doc")
    root = ChunkNode(chunk=_chunk(), children=[child_a, child_b])

    restored = node_from_dict(node_to_dict(root))

    assert restored == root
    assert restored.children is not None
    assert [c.chunk.path for c in restored.children] == ["A-1", "A-2"]


def test_node_to_dict_and_from_dict_round_trip_escalated():
    node = ChunkNode(chunk=_chunk(), escalation_reason="trivial-breakdown deadlock")
    restored = node_from_dict(node_to_dict(node))
    assert restored == node


def test_write_tree_then_load_tree_round_trips(tmp_path):
    child = ChunkNode(chunk=_chunk("A-1", "first", depth=1), leaf_doc="first doc")
    root = ChunkNode(chunk=_chunk(), children=[child])
    path = tmp_path / "tree.json"

    write_tree(root, path)
    restored = load_tree(path)

    assert restored == root


def test_write_tree_preserves_hand_edits_on_reload(tmp_path):
    child_a = ChunkNode(chunk=_chunk("A-1", "first", depth=1), leaf_doc="first doc")
    child_b = ChunkNode(chunk=_chunk("A-2", "second", depth=1), leaf_doc="second doc")
    root = ChunkNode(chunk=_chunk(), children=[child_a, child_b])
    path = tmp_path / "tree.json"
    write_tree(root, path)

    data = node_to_dict(root)
    data["children"] = [data["children"][1], data["children"][0]]
    path.write_text(json.dumps(data))

    restored = load_tree(path)
    assert restored.children is not None
    assert [c.chunk.path for c in restored.children] == ["A-2", "A-1"]


def test_load_tree_raises_when_file_missing(tmp_path):
    with pytest.raises(DecomposeError, match="not found"):
        load_tree(tmp_path / "missing.json")


def test_load_tree_raises_on_invalid_json(tmp_path):
    path = tmp_path / "tree.json"
    path.write_text("{not json")
    with pytest.raises(DecomposeError, match="not valid JSON"):
        load_tree(path)
