from __future__ import annotations

from pathlib import Path

import pytest

from sisyphus.archive import check_archive_dir, resolve_archive_dir
from sisyphus.config import ConfigError


def test_absolute_dir(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    assert resolve_archive_dir(str(other), str(tmp_path / "ws")) == other
    assert resolve_archive_dir(str(other), None) == other


def test_relative_dir(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert resolve_archive_dir("archive", str(ws)) == ws / "archive"


def test_relative_dir_without_workspace() -> None:
    with pytest.raises(ConfigError, match="workspace_dir"):
        resolve_archive_dir("archive", None)


def test_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_archive_dir("~/arch", "~/ws") == tmp_path / "arch"
    assert resolve_archive_dir("arch", "~/ws") == tmp_path / "ws" / "arch"


def test_resolver_creates_nothing(tmp_path: Path) -> None:
    resolve_archive_dir("archive", str(tmp_path / "ws"))
    assert list(tmp_path.iterdir()) == []


def test_check_ok(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    check_archive_dir(ws / "archive", str(ws))
    check_archive_dir(tmp_path / "other", str(ws))
    assert list(tmp_path.iterdir()) == []


def test_check_equal(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ConfigError):
        check_archive_dir(ws, str(ws))
    with pytest.raises(ConfigError):
        check_archive_dir(ws / "sub" / "..", str(ws))


def test_check_contains(tmp_path: Path) -> None:
    ws = tmp_path / "a" / "ws"
    ws.mkdir(parents=True)
    with pytest.raises(ConfigError):
        check_archive_dir(tmp_path, str(ws))
    with pytest.raises(ConfigError):
        check_archive_dir(tmp_path / "a", str(ws))


def test_check_symlink_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    link = tmp_path / "link"
    link.symlink_to(ws)
    with pytest.raises(ConfigError):
        check_archive_dir(link, str(ws))


def test_check_unset_workspace(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="workspace_dir"):
        check_archive_dir(tmp_path, None)
