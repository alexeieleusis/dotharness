"""Tests for git path helpers."""

from pathlib import Path

from dotharness.vibe_heal.git.paths import to_repo_relative


class TestToRepoRelative:
    def test_absolute_path_inside_repo(self, tmp_path: Path) -> None:
        assert to_repo_relative(tmp_path / "src" / "a.py", tmp_path) == "src/a.py"

    def test_cwd_relative_path(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "src").mkdir()
        monkeypatch.chdir(tmp_path / "src")
        assert to_repo_relative(Path("a.py"), tmp_path) == "src/a.py"

    def test_path_outside_repo_falls_back_to_posix(self, tmp_path: Path) -> None:
        assert to_repo_relative(Path("/elsewhere/a.py"), tmp_path) == "/elsewhere/a.py"
