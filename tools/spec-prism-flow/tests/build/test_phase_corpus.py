"""Tests for `discover_phase_files`/`merged_phase_numbers`."""

from conftest import make_phase as _phase
from conftest import write_phase_file as _write_phase_file

from spec_prism_flow.build.phase_corpus import discover_phase_files

# --- discover_phase_files --------------------------------------------------------


def test_discover_phase_files_sorts_numerically_not_lexicographically(tmp_path):
    phases_dir = tmp_path / "phases"
    for phase in [_phase(number=2, name="two"), _phase(number=9, name="nine"), _phase(number=10, name="ten")]:
        _write_phase_file(phases_dir, phase)

    paths = discover_phase_files(phases_dir)

    assert [p.name for p in paths] == ["02-two-leaf.md", "09-nine-leaf.md", "10-ten-leaf.md"]


def test_discover_phase_files_ignores_non_matching_names(tmp_path):
    phases_dir = tmp_path / "phases"
    _write_phase_file(phases_dir, _phase(number=1, name="one"))
    (phases_dir / "graph.json").write_text("{}")
    (phases_dir / "AB-not-numbered-leaf.md").write_text("not a phase file")
    (phases_dir / "notes.md").write_text("not a leaf file")

    paths = discover_phase_files(phases_dir)

    assert [p.name for p in paths] == ["01-one-leaf.md"]


def test_discover_phase_files_empty_dir_returns_empty_list(tmp_path):
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    assert discover_phase_files(phases_dir) == []
