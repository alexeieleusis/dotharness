import json

import pytest

from spec_prism_flow import decompose, generator
from spec_prism_flow.chunk import Chunk, load_tree
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
from spec_prism_flow.workspace import init_workspace


def _make_cfg(tmp_path) -> SpecPrismFlowConfig:
    return SpecPrismFlowConfig(
        agent=AgentConfig(),
        plan=PlanConfig(workspace_dir=tmp_path / "workspace", phase_dir=tmp_path / "phases"),
        review=ReviewConfig(),
        vibe_heal=VibeHealConfig(),
        build=BuildConfig(),
        harness=HarnessSection(knowledge_dir=tmp_path / "knowledge"),
    )


def _in_band_doc() -> str:
    return " ".join(["word"] * 600)


def _in_band_scope(prefix: str) -> list[str]:
    return [f"{prefix}{i}.py" for i in range(6)]


def _leaf_chunk(path="A-1", name="stuck", depth=1, file_scope_estimate=None) -> Chunk:
    return Chunk(
        path=path,
        name=name,
        file_scope_estimate=file_scope_estimate if file_scope_estimate is not None else ["only_one.py"],
        requirements_slice="requirements slice",
        depth=depth,
    )


def _log_entries(log_path) -> list[dict]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines()]


# --- resolve_chunk -----------------------------------------------------------------------------


def test_resolve_chunk_returns_leaf_directly_when_in_band(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    chunk_obj = _leaf_chunk(file_scope_estimate=_in_band_scope("f"))
    monkeypatch.setattr(
        decompose.generator, "run_generator", lambda c, cfg_arg, *, forced_split=False: generator.Leaf(_in_band_doc())
    )
    log_path = tmp_path / "log.jsonl"

    node = decompose.resolve_chunk(chunk_obj, cfg, depth_cap=4, log_path=log_path)

    assert node.is_leaf
    assert not log_path.exists()


def test_resolve_chunk_retries_once_then_escalates_when_still_leaf_after_forced_split(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    chunk_obj = _leaf_chunk()
    calls = []

    def _fake(c, cfg_arg, *, forced_split=False):
        calls.append(forced_split)
        return generator.Leaf("too short")

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    log_path = tmp_path / "log.jsonl"

    node = decompose.resolve_chunk(chunk_obj, cfg, depth_cap=4, log_path=log_path)

    assert calls == [False, True]
    assert node.is_escalated
    assert node.escalation_reason is not None
    assert "Trivial-breakdown deadlock" in node.escalation_reason

    entries = _log_entries(log_path)
    assert [e["event"] for e in entries] == ["retry", "escalate"]
    assert all(e["chunk_path"] == "A-1" for e in entries)


def test_resolve_chunk_retries_then_recurses_into_forced_split_children(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    chunk_obj = _leaf_chunk()
    child_a = Chunk(
        path="A-1-1", name="child-a", file_scope_estimate=_in_band_scope("f"), requirements_slice="a", depth=2
    )
    child_b = Chunk(
        path="A-1-2", name="child-b", file_scope_estimate=_in_band_scope("g"), requirements_slice="b", depth=2
    )

    def _fake(c, cfg_arg, *, forced_split=False):
        if c.path == "A-1" and not forced_split:
            return generator.Leaf("too short")
        if c.path == "A-1" and forced_split:
            return generator.Split(children=[child_a, child_b])
        return generator.Leaf(_in_band_doc())

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    log_path = tmp_path / "log.jsonl"

    node = decompose.resolve_chunk(chunk_obj, cfg, depth_cap=4, log_path=log_path)

    assert node.children is not None
    assert [c.chunk.path for c in node.children] == ["A-1-1", "A-1-2"]
    assert [c.is_leaf for c in node.children] == [True, True]

    entries = _log_entries(log_path)
    assert [e["event"] for e in entries] == ["retry"]


def test_resolve_chunk_escalates_on_natural_split_at_depth_cap(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    chunk_obj = _leaf_chunk(path="A-1-1-1", name="deep", depth=4, file_scope_estimate=[])
    monkeypatch.setattr(
        decompose.generator,
        "run_generator",
        lambda c, cfg_arg, *, forced_split=False: generator.Split(
            children=[
                Chunk(path="x-1", name="c1", file_scope_estimate=[], requirements_slice="a", depth=5),
                Chunk(path="x-2", name="c2", file_scope_estimate=[], requirements_slice="b", depth=5),
            ]
        ),
    )
    log_path = tmp_path / "log.jsonl"

    node = decompose.resolve_chunk(chunk_obj, cfg, depth_cap=4, log_path=log_path)

    assert node.is_escalated
    assert node.escalation_reason is not None
    assert "Depth cap" in node.escalation_reason
    entries = _log_entries(log_path)
    assert entries[0]["event"] == "escalate"
    assert entries[0]["chunk_path"] == "A-1-1-1"


def test_resolve_chunk_escalates_when_forced_split_retry_hits_depth_cap(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    chunk_obj = _leaf_chunk(path="A-1-1-1", name="deep", depth=4)

    def _fake(c, cfg_arg, *, forced_split=False):
        if not forced_split:
            return generator.Leaf("too short")
        return generator.Split(
            children=[
                Chunk(path="x-1", name="c1", file_scope_estimate=[], requirements_slice="a", depth=5),
                Chunk(path="x-2", name="c2", file_scope_estimate=[], requirements_slice="b", depth=5),
            ]
        )

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    log_path = tmp_path / "log.jsonl"

    node = decompose.resolve_chunk(chunk_obj, cfg, depth_cap=4, log_path=log_path)

    assert node.is_escalated
    assert node.escalation_reason is not None
    assert "Depth cap" in node.escalation_reason
    entries = _log_entries(log_path)
    assert [e["event"] for e in entries] == ["retry", "escalate"]


def test_resolve_chunk_processes_split_children_left_to_right_to_completion(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    calls = []

    def _fake(c, cfg_arg, *, forced_split=False):
        calls.append(c.path)
        if c.path == "A":
            return generator.Split(
                children=[
                    Chunk(path="A-1", name="first", file_scope_estimate=[], requirements_slice="a", depth=1),
                    Chunk(
                        path="A-2",
                        name="second",
                        file_scope_estimate=_in_band_scope("g"),
                        requirements_slice="b",
                        depth=1,
                    ),
                ]
            )
        if c.path == "A-1":
            return generator.Split(
                children=[
                    Chunk(
                        path="A-1-1",
                        name="nested",
                        file_scope_estimate=_in_band_scope("f"),
                        requirements_slice="c",
                        depth=2,
                    )
                ]
            )
        return generator.Leaf(_in_band_doc())

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    root_chunk = Chunk(path="A", name="root", file_scope_estimate=[], requirements_slice="root", depth=0)
    log_path = tmp_path / "log.jsonl"

    decompose.resolve_chunk(root_chunk, cfg, depth_cap=4, log_path=log_path)

    assert calls == ["A", "A-1", "A-1-1", "A-2"]


# --- run_decompose (end-to-end, stubbed generator) ----------------------------------------------


def _init_workspace_with_requirements(cfg, requirements_text="full requirements text") -> None:
    brief = cfg.plan.workspace_dir.parent / "brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text("brief content")
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])
    (cfg.plan.workspace_dir / "requirements.md").write_text(requirements_text)


def test_run_decompose_raises_when_manifest_missing(tmp_path):
    cfg = _make_cfg(tmp_path)
    cfg.plan.workspace_dir.mkdir(parents=True)

    with pytest.raises(DecomposeError, match="Manifest not found"):
        decompose.run_decompose(cfg)


def test_run_decompose_raises_when_requirements_missing(tmp_path):
    cfg = _make_cfg(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("brief content")
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])

    with pytest.raises(DecomposeError, match="Requirements doc not found"):
        decompose.run_decompose(cfg)


def test_run_decompose_writes_tree_and_graph_with_split_then_leaf_generator(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _init_workspace_with_requirements(cfg)

    def _fake(c, cfg_arg, *, forced_split=False):
        if c.path == "A":
            return generator.Split(
                children=[
                    Chunk(
                        path="A-1",
                        name="first",
                        file_scope_estimate=_in_band_scope("f"),
                        requirements_slice="a",
                        depth=1,
                    ),
                    Chunk(
                        path="A-2",
                        name="second",
                        file_scope_estimate=_in_band_scope("g"),
                        requirements_slice="b",
                        depth=1,
                    ),
                ]
            )
        return generator.Leaf(_in_band_doc())

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    monkeypatch.setattr(decompose.click, "confirm", lambda *a, **k: True)

    tree_path, graph_path = decompose.run_decompose(cfg)

    assert tree_path.exists()
    assert graph_path.exists()

    tree = load_tree(tree_path)
    assert tree.children is not None
    assert [c.chunk.path for c in tree.children] == ["A-1", "A-2"]

    graph_data = json.loads(graph_path.read_text())
    assert graph_data["nodes"] == ["01-first-leaf", "02-second-leaf"]


def test_run_decompose_reads_tree_from_disk_after_checkpoint_honoring_hand_edits(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _init_workspace_with_requirements(cfg)

    def _fake(c, cfg_arg, *, forced_split=False):
        if c.path == "A":
            return generator.Split(
                children=[
                    Chunk(
                        path="A-1",
                        name="first",
                        file_scope_estimate=_in_band_scope("f"),
                        requirements_slice="a",
                        depth=1,
                    ),
                    Chunk(
                        path="A-2",
                        name="second",
                        file_scope_estimate=_in_band_scope("g"),
                        requirements_slice="b",
                        depth=1,
                    ),
                ]
            )
        return generator.Leaf(_in_band_doc())

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)

    def _fake_confirm(*a, **k):
        tree_path = cfg.plan.workspace_dir / decompose.TREE_FILENAME
        data = json.loads(tree_path.read_text())
        data["children"] = list(reversed(data["children"]))
        tree_path.write_text(json.dumps(data))
        return True

    monkeypatch.setattr(decompose.click, "confirm", _fake_confirm)

    tree_path, _graph_path = decompose.run_decompose(cfg)

    result_tree = load_tree(tree_path)
    assert result_tree.children is not None
    assert [c.chunk.path for c in result_tree.children] == ["A-2", "A-1"]


def test_run_decompose_does_not_reinvoke_generator_after_checkpoint(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _init_workspace_with_requirements(cfg)

    call_count = {"n": 0}

    def _fake(c, cfg_arg, *, forced_split=False):
        call_count["n"] += 1
        if c.path == "A":
            return generator.Split(
                children=[
                    Chunk(
                        path="A-1",
                        name="first",
                        file_scope_estimate=_in_band_scope("f"),
                        requirements_slice="a",
                        depth=1,
                    ),
                    Chunk(
                        path="A-2",
                        name="second",
                        file_scope_estimate=_in_band_scope("g"),
                        requirements_slice="b",
                        depth=1,
                    ),
                ]
            )
        return generator.Leaf(_in_band_doc())

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    monkeypatch.setattr(decompose.click, "confirm", lambda *a, **k: True)

    decompose.run_decompose(cfg)

    assert call_count["n"] == 3  # root + 2 leaves; nothing extra after the checkpoint pause


def test_run_decompose_links_leaves_that_share_file_scope_with_no_cross_reference(tmp_path, monkeypatch):
    cfg = _make_cfg(tmp_path)
    _init_workspace_with_requirements(cfg)

    def _fake(c, cfg_arg, *, forced_split=False):
        if c.path == "A":
            return generator.Split(
                children=[
                    Chunk(
                        path="A-1",
                        name="first",
                        file_scope_estimate=["src/shared.py", *_in_band_scope("a-extra")],
                        requirements_slice="a",
                        depth=1,
                    ),
                    Chunk(
                        path="A-2",
                        name="second",
                        file_scope_estimate=_in_band_scope("b-extra"),
                        requirements_slice="b",
                        depth=1,
                    ),
                    Chunk(
                        path="A-3",
                        name="third",
                        file_scope_estimate=["src/shared.py", *_in_band_scope("c-extra")],
                        requirements_slice="c",
                        depth=1,
                    ),
                ]
            )
        return generator.Leaf(f"standalone doc for {c.path}, no cross references, " + _in_band_doc())

    monkeypatch.setattr(decompose.generator, "run_generator", _fake)
    monkeypatch.setattr(decompose.click, "confirm", lambda *a, **k: True)

    _, graph_path = decompose.run_decompose(cfg)

    graph_data = json.loads(graph_path.read_text())
    assert ["03-third-leaf", "01-first-leaf"] in graph_data["edges"]
    assert ["02-second-leaf", "01-first-leaf"] not in graph_data["edges"]
