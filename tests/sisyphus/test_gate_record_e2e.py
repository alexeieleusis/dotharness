"""Harness: the sisyphus writer and the pr_review reader share one data dir.

Lives in ``tests/`` so it may import both sides; the import-boundary tests scan sisyphus source only.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dotharness.pr_review.runners.merge import gate_record_path, read_gate_record
from dotharness.sisyphus.gaterecord import write_gate_record

REPO_SLUG = "acme-frontend"
PR_NUMBER = 42
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def shared_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, str, int]]:
    """Redirect writer and reader to one temp data dir; yield (data_dir, repo_slug, pr_number).

    Both sides resolve through ``dotharness.paths.data_dir()`` at call time, which calls ``Path.home()``,
    so one ``Path.home`` patch moves both. Undone by ``monkeypatch`` after each test.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    yield home / ".local" / "share" / "dotharness", REPO_SLUG, PR_NUMBER


def test_writer_and_reader_resolve_the_same_path(shared_data_dir: tuple[Path, str, int]) -> None:
    data_dir, slug, pr = shared_data_dir

    writer_path = write_gate_record(slug, pr, HEAD_SHA)
    reader_path = gate_record_path(slug, pr)

    assert writer_path == reader_path
    assert writer_path.is_relative_to(data_dir)
    assert writer_path.is_file()
    assert read_gate_record(slug, pr) == HEAD_SHA
