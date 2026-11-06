from dotharness.spec_prism_flow import generator
from dotharness.spec_prism_flow.chunk import Chunk
from dotharness.spec_prism_flow.decompose_journal import JOURNAL_FILENAME, DecomposeJournal


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


def test_journal_append_after_torn_line_is_not_lost(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    chunk = _chunk()
    DecomposeJournal(path).record(chunk, forced_split=False, result=generator.Leaf("x"))
    with open(path, "a") as f:
        f.write('{"event": "resu')
    other = _chunk("B")
    DecomposeJournal(path).record(other, forced_split=False, result=generator.Leaf("y"))

    reloaded = DecomposeJournal(path)
    assert reloaded.lookup(chunk, forced_split=False) == generator.Leaf("x")
    assert reloaded.lookup(other, forced_split=False) == generator.Leaf("y")


def test_journal_skips_valid_json_but_malformed_lines(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    path.write_text('[1]\n{"event": "result"}\n{"event": "result", "chunk_path": "A", "forced_split": false}\n')

    assert DecomposeJournal(path).result_count == 0


def test_journal_discard_removes_file(tmp_path):
    path = tmp_path / JOURNAL_FILENAME
    journal = DecomposeJournal(path)
    journal.mark_tree_written(_chunk(), 4)
    journal.discard()

    assert not path.exists()
    assert not journal.tree_written
