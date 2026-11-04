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

    def test_symlinked_repo_root_with_resolved_absolute_path(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert to_repo_relative(real.resolve() / "a.py", link) == "a.py"

    def test_unnormalised_repo_root(self, tmp_path: Path) -> None:
        (tmp_path / "repo").mkdir()
        root = tmp_path / "repo" / ".." / "repo"
        assert to_repo_relative(tmp_path / "repo" / "a.py", root) == "a.py"

    def test_repo_relative_path_from_subdirectory_cwd(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text("")
        monkeypatch.chdir(tmp_path / "src")
        assert to_repo_relative(Path("src/a.py"), tmp_path) == "src/a.py"

    def test_relative_path_escaping_repo_logs_warning(self, tmp_path: Path, monkeypatch, caplog) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        with caplog.at_level("WARNING"):
            assert to_repo_relative(Path("../x.py"), repo) == "../x.py"
        assert "outside repository root" in caplog.text

    def test_outside_repo_absolute_logs_warning(self, tmp_path: Path, caplog) -> None:
        with caplog.at_level("WARNING"):
            to_repo_relative(Path("/elsewhere/a.py"), tmp_path)
        assert "outside repository root" in caplog.text
