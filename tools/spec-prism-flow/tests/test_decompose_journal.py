import contextlib
import json

from spec_prism_flow import decompose, generator
from spec_prism_flow.chunk import Chunk, load_tree
from spec_prism_flow.decompose_journal import JOURNAL_FILENAME, DecomposeJournal
from spec_prism_flow.workspace import init_workspace
from tests.test_decompose import _in_band_doc, _in_band_scope, _make_cfg


def _oversized_doc() -> str:
    return " ".join(["word"] * 3000)


def _chunk(path="A", slice_text="slice", depth=0) -> Chunk:
    return Chunk(path=path, name="n", file_scope_estimate=["a.py"], requirements_slice=slice_text, depth=depth)


def test_journal_round_trips_leaf_and_split(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    root, child = _chunk(), _chunk("A-1", "c", 1)
    journal = DecomposeJournal(path)
    journal.record(root, forced_split=False, result=generator.Split(children=[child, _chunk("A-2", "d", 1)]))
    journal.record(child, forced_split=False, result=generator.Leaf("doc"))

    reloaded = DecomposeJournal(path)

    assert reloaded.lookup(child, forced_split=False) == generator.Leaf("doc")
    split = reloaded.lookup(root, forced_split=False)
    assert isinstance(split, generator.Split)
    assert [c.path for c in split.children] == ["A-1", "A-2"]


def test_journal_keys_forced_split_separately(tmp_path):
    journal = DecomposeJournal(tmp_path / JOURNAL_FILENAME)
    chunk = _chunk()
    journal.record(chunk, forced_split=False, result=generator.Leaf("x"))

    assert journal.lookup(chunk, forced_split=True) is None


def test_journal_ignores_result_when_slice_changed(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    DecomposeJournal(path).record(_chunk(slice_text="old"), forced_split=False, result=generator.Leaf("x"))

    assert DecomposeJournal(path).lookup(_chunk(slice_text="new"), forced_split=False) is None


def test_journal_skips_torn_final_line(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    chunk = _chunk()
    DecomposeJournal(path).record(chunk, forced_split=False, result=generator.Leaf("x"))
    with open(path, "a") as f:
        f.write('{"event": "result", "chunk_pa')

    assert DecomposeJournal(path).lookup(chunk, forced_split=False) == generator.Leaf("x")


def test_journal_discard_removes_file(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    journal = DecomposeJournal(path)
    journal.mark_tree_written()
    journal.discard()

    assert not path.exists()
    assert not journal.tree_written


# --- run_decompose resume ----------------------------------------------------------------------


def _setup(tmp_path, monkeypatch, confirmations=None):
    cfg = _make_cfg(tmp_path)
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    init_workspace(cfg.plan.workspace_dir, brief, None, None, [])
    (cfg.plan.workspace_dir / "requirements.md").write_text("root requirements")
    monkeypatch.setattr(decompose.handoff, "wait_for_confirmation", lambda prompt: None)
    return cfg


def _two_leaf_generator(calls, fail_on=None):
    def _fake(chunk, cfg, *, forced_split=False):
        calls.append(chunk.path)
        if chunk.path == fail_on:
            raise KeyboardInterrupt
        if chunk.path == "A":
            return generator.Split(
                children=[
                    Chunk("A-1", "one", _in_band_scope("f"), "s1", 1),
                    Chunk("A-2", "two", _in_band_scope("g"), "s2", 1),
                ]
            )
        return generator.Leaf(_in_band_doc())

    return _fake


def test_resume_skips_recorded_generator_calls(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    first_calls: list[str] = []
    monkeypatch.setattr(decompose.generator, "run_generator", _two_leaf_generator(first_calls, fail_on="A-2"))
    with contextlib.suppress(KeyboardInterrupt):
        decompose.run_decompose(cfg)
    assert first_calls == ["A", "A-1", "A-2"]

    second_calls: list[str] = []
    monkeypatch.setattr(decompose.generator, "run_generator", _two_leaf_generator(second_calls))
    tree_path, graph_path = decompose.run_decompose(cfg, resume=True)

    assert second_calls == ["A-2"]
    assert graph_path.exists()
    assert [c.chunk.path for c in load_tree(tree_path).children or []] == ["A-1", "A-2"]
    assert not (cfg.plan.workspace_dir / JOURNAL_FILENAME).exists()


def test_without_resume_discards_journal_and_regenerates(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(decompose.generator, "run_generator", _two_leaf_generator([], fail_on="A-2"))
    with contextlib.suppress(KeyboardInterrupt):
        decompose.run_decompose(cfg)

    calls: list[str] = []
    monkeypatch.setattr(decompose.generator, "run_generator", _two_leaf_generator(calls))
    decompose.run_decompose(cfg)

    assert calls == ["A", "A-1", "A-2"]


def test_resume_does_not_duplicate_log_entries(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)

    def _make(interrupt):
        def _fake(chunk, cfg_arg, *, forced_split=False):
            if chunk.path == "A":
                return generator.Split(
                    children=[
                        Chunk("A-1", "one", _in_band_scope("f"), "s1", 1),
                        Chunk("A-2", "two", _in_band_scope("g"), "s2", 1),
                    ]
                )
            if chunk.path == "A-1" and not forced_split:
                return generator.Leaf(_oversized_doc())
            if chunk.path == "A-1":
                return generator.Split(
                    children=[
                        Chunk("A-1-1", "x", _in_band_scope("h"), "s3", 2),
                        Chunk("A-1-2", "y", _in_band_scope("i"), "s4", 2),
                    ]
                )
            if chunk.path == "A-2" and interrupt:
                raise KeyboardInterrupt
            return generator.Leaf(_in_band_doc())

        return _fake

    monkeypatch.setattr(decompose.generator, "run_generator", _make(interrupt=True))
    with contextlib.suppress(KeyboardInterrupt):
        decompose.run_decompose(cfg)
    monkeypatch.setattr(decompose.generator, "run_generator", _make(interrupt=False))
    decompose.run_decompose(cfg, resume=True)

    entries = [
        json.loads(line)
        for line in (cfg.plan.workspace_dir / decompose.DECOMPOSE_LOG_FILENAME).read_text().splitlines()
    ]
    assert [(e["chunk_path"], e["event"]) for e in entries] == [("A-1", "retry")]


def test_resume_after_tree_written_keeps_hand_edited_tree(tmp_path, monkeypatch):
    cfg = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(decompose.generator, "run_generator", _two_leaf_generator([]))

    def _die_at_review(prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(decompose.handoff, "wait_for_confirmation", _die_at_review)
    with contextlib.suppress(KeyboardInterrupt):
        decompose.run_decompose(cfg)

    tree_path = cfg.plan.workspace_dir / decompose.TREE_FILENAME
    data = json.loads(tree_path.read_text())
    data["children"].reverse()
    tree_path.write_text(json.dumps(data))

    calls: list[str] = []
    monkeypatch.setattr(decompose.generator, "run_generator", _two_leaf_generator(calls))
    monkeypatch.setattr(decompose.handoff, "wait_for_confirmation", lambda prompt: None)
    decompose.run_decompose(cfg, resume=True)

    assert calls == []
    assert [c["chunk"]["path"] for c in json.loads(tree_path.read_text())["children"]] == ["A-2", "A-1"]
