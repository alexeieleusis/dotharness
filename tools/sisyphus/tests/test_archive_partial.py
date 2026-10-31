import errno
import shutil
from pathlib import Path

import pytest

from sisyphus import archive


def test_interrupted_copy_leaves_no_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "a.txt"
    src.write_text("hello")
    dest_dir = tmp_path / "arch"
    dest_dir.mkdir()
    dest = dest_dir / "a.txt"

    def boom(s, d, **kw):
        Path(d).write_text("he")
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(shutil, "copy2", boom)
    with pytest.raises(OSError):
        archive._copy_then_delete(src, dest, [])
    assert src.exists()
    assert not dest.exists()
    assert not archive._partial_path(dest).exists()
