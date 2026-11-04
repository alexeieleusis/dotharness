"""Tests for BranchAnalyzer class."""

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from git import GitCommandError, Repo
from git.exc import InvalidGitRepositoryError

from dotharness.vibe_heal.git.branch_analyzer import (
    BranchAnalyzer,
    BranchAnalyzerError,
    BranchNotFoundError,
    InvalidRepositoryError,
)


@pytest.fixture
def mock_repo(tmp_path: Path) -> MagicMock:
    """Create a mock Git repository."""
    repo = MagicMock(spec=Repo)
    repo.working_dir = str(tmp_path)
    return repo


@pytest.fixture
def branch_analyzer(tmp_path: Path, mock_repo: MagicMock) -> BranchAnalyzer:
    """Create a BranchAnalyzer instance with mocked repo."""
    with patch("dotharness.vibe_heal.git.branch_analyzer.Repo", return_value=mock_repo):
        analyzer = BranchAnalyzer(tmp_path)
    return analyzer


def _repo(analyzer: BranchAnalyzer) -> MagicMock:
    """Return the analyzer's (mocked) repo typed as a mock so attributes can be configured."""
    return cast("MagicMock", analyzer.repo)


class TestBranchAnalyzerInit:
    """Tests for BranchAnalyzer initialization."""

    def test_init_valid_repo(self, tmp_path: Path, mock_repo: MagicMock) -> None:
        """Test initialization with valid repository."""
        with patch("dotharness.vibe_heal.git.branch_analyzer.Repo", return_value=mock_repo):
            analyzer = BranchAnalyzer(tmp_path)

        assert analyzer.repo == mock_repo
        assert analyzer.repo_path == tmp_path

    def test_init_invalid_repo(self, tmp_path: Path) -> None:
        """Test initialization with invalid repository raises error."""
        with (
            patch(
                "dotharness.vibe_heal.git.branch_analyzer.Repo",
                side_effect=InvalidGitRepositoryError("Not a git repo"),
            ),
            pytest.raises(InvalidRepositoryError, match="Not a valid git repository"),
        ):
            BranchAnalyzer(tmp_path)

    def test_init_searches_parent_directories(self, tmp_path: Path, mock_repo: MagicMock) -> None:
        """Test that initialization searches parent directories for git repo."""
        with patch("dotharness.vibe_heal.git.branch_analyzer.Repo", return_value=mock_repo) as mock_repo_cls:
            BranchAnalyzer(tmp_path)

        mock_repo_cls.assert_called_once_with(tmp_path, search_parent_directories=True)


class TestGetModifiedFiles:
    """Tests for get_modified_files method."""

    def test_get_modified_files_default_base_branch(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test get_modified_files uses origin/main as default."""
        # Create test files
        file1 = tmp_path / "file1.py"
        file1.write_text("content")

        branch_analyzer.repo.git.diff.return_value = "file1.py"

        # Mock branch validation
        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        # Verify default base branch is used
        branch_analyzer.repo.git.diff.assert_called_once_with("--name-only", "origin/main...HEAD")
        assert files == [Path("file1.py")]

    def test_get_modified_files_custom_base_branch(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test get_modified_files with custom base branch."""
        file1 = tmp_path / "file1.py"
        file1.write_text("content")

        branch_analyzer.repo.git.diff.return_value = "file1.py"

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files("develop")

        branch_analyzer.repo.git.diff.assert_called_once_with("--name-only", "develop...HEAD")
        assert files == [Path("file1.py")]

    def test_get_modified_files_multiple_files(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test get_modified_files with multiple modified files."""
        # Create test files
        file1 = tmp_path / "file1.py"
        file2 = tmp_path / "file2.py"
        file1.write_text("content")
        file2.write_text("content")

        branch_analyzer.repo.git.diff.return_value = "file1.py\nfile2.py"

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        assert len(files) == 2
        assert Path("file1.py") in files
        assert Path("file2.py") in files

    def test_get_modified_files_filters_deleted_files(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test that deleted files are excluded from results."""
        # Create only one file (file1 exists, file2 deleted)
        file1 = tmp_path / "file1.py"
        file1.write_text("content")

        # Git diff shows both files, but file2.py doesn't exist
        branch_analyzer.repo.git.diff.return_value = "file1.py\nfile2.py"

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        # Only existing file should be returned
        assert files == [Path("file1.py")]

    def test_get_modified_files_nested_paths(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test get_modified_files with nested directory paths."""
        # Create nested file
        nested_dir = tmp_path / "src" / "modules"
        nested_dir.mkdir(parents=True)
        file1 = nested_dir / "file1.py"
        file1.write_text("content")

        branch_analyzer.repo.git.diff.return_value = "src/modules/file1.py"

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        assert files == [Path("src/modules/file1.py")]

    def test_get_modified_files_no_changes(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test get_modified_files when no files are modified."""
        branch_analyzer.repo.git.diff.return_value = ""

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        assert files == []

    def test_get_modified_files_whitespace_handling(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test that whitespace in diff output is handled correctly."""
        file1 = tmp_path / "file1.py"
        file1.write_text("content")

        # Diff output with extra whitespace
        branch_analyzer.repo.git.diff.return_value = "\nfile1.py\n\n"

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        assert files == [Path("file1.py")]

    def test_get_modified_files_branch_not_found(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test error when base branch doesn't exist."""
        with (
            patch.object(branch_analyzer, "validate_branch_exists", return_value=False),
            pytest.raises(BranchNotFoundError, match="Branch 'nonexistent' does not exist"),
        ):
            branch_analyzer.get_modified_files("nonexistent")

    def test_get_modified_files_git_command_error(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test error handling when git diff command fails."""
        branch_analyzer.repo.git.diff.side_effect = GitCommandError("git diff", 1)

        with (
            patch.object(branch_analyzer, "validate_branch_exists", return_value=True),
            pytest.raises(BranchAnalyzerError, match="Git diff command failed"),
        ):
            branch_analyzer.get_modified_files()

    def test_get_modified_files_filters_directories(self, branch_analyzer: BranchAnalyzer, tmp_path: Path) -> None:
        """Test that directories are filtered out, only files are returned."""
        # Create a directory and a file
        dir1 = tmp_path / "dir1"
        dir1.mkdir()
        file1 = tmp_path / "file1.py"
        file1.write_text("content")

        # Diff shows both (though git diff typically only shows files)
        branch_analyzer.repo.git.diff.return_value = "dir1\nfile1.py"

        with patch.object(branch_analyzer, "validate_branch_exists", return_value=True):
            files = branch_analyzer.get_modified_files()

        # Only file should be returned
        assert files == [Path("file1.py")]

    def test_get_modified_files_repo_root_behavior_preserved(
        self, branch_analyzer: BranchAnalyzer, tmp_path: Path
    ) -> None:
        """Test that paths are unchanged when CWD equals repo root."""
        file1 = tmp_path / "src" / "main.py"
        file1.parent.mkdir(parents=True)
        file1.write_text("content")

        branch_analyzer.repo.git.diff.return_value = "src/main.py"

        with (
            patch.object(branch_analyzer, "validate_branch_exists", return_value=True),
            patch("dotharness.vibe_heal.git.branch_analyzer.Path.cwd", return_value=tmp_path),
        ):
            files = branch_analyzer.get_modified_files()

        assert files == [Path("src/main.py")]

    def test_get_modified_files_monorepo_subdir_path_stripped(
        self, branch_analyzer: BranchAnalyzer, tmp_path: Path
    ) -> None:
        """Test that paths are stripped to CWD-relative when running from a monorepo subdirectory."""
        # Simulate repo root at tmp_path, CWD at tmp_path/foo
        subdir = tmp_path / "foo"
        file1 = subdir / "src" / "main.py"
        file1.parent.mkdir(parents=True)
        file1.write_text("content")

        # Git diff returns repo-relative path
        branch_analyzer.repo.git.diff.return_value = "foo/src/main.py"

        with (
            patch.object(branch_analyzer, "validate_branch_exists", return_value=True),
            patch("dotharness.vibe_heal.git.branch_analyzer.Path.cwd", return_value=subdir),
        ):
            files = branch_analyzer.get_modified_files()

        # Should be relative to CWD (foo/), not repo root
        assert files == [Path("src/main.py")]

    def test_get_modified_files_monorepo_subdir_cross_project_filtered(
        self, branch_analyzer: BranchAnalyzer, tmp_path: Path
    ) -> None:
        """Test that files from other monorepo projects are excluded when running from a subdir."""
        # CWD is tmp_path/foo, but bar/file.py is in a sibling project
        subdir = tmp_path / "foo"
        subdir.mkdir()
        foo_file = subdir / "main.py"
        foo_file.write_text("content")

        bar_dir = tmp_path / "bar"
        bar_file = bar_dir / "main.py"
        bar_file.parent.mkdir(parents=True)
        bar_file.write_text("content")

        # Git diff returns both files
        branch_analyzer.repo.git.diff.return_value = "foo/main.py\nbar/main.py"

        with (
            patch.object(branch_analyzer, "validate_branch_exists", return_value=True),
            patch("dotharness.vibe_heal.git.branch_analyzer.Path.cwd", return_value=subdir),
        ):
            files = branch_analyzer.get_modified_files()

        # Only foo/main.py should be returned, as bar/ is outside CWD
        assert files == [Path("main.py")]

    def test_get_modified_files_monorepo_deleted_files_still_excluded(
        self, branch_analyzer: BranchAnalyzer, tmp_path: Path
    ) -> None:
        """Test that deleted files are excluded even in monorepo subdir mode."""
        subdir = tmp_path / "foo"
        existing_file = subdir / "main.py"
        existing_file.parent.mkdir(parents=True)
        existing_file.write_text("content")

        # Git diff shows two files, but deleted.py doesn't exist on disk
        branch_analyzer.repo.git.diff.return_value = "foo/main.py\nfoo/deleted.py"

        with (
            patch.object(branch_analyzer, "validate_branch_exists", return_value=True),
            patch("dotharness.vibe_heal.git.branch_analyzer.Path.cwd", return_value=subdir),
        ):
            files = branch_analyzer.get_modified_files()

        assert files == [Path("main.py")]


class TestGetCurrentBranch:
    """Tests for get_current_branch method."""

    def test_get_current_branch_success(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test getting current branch name."""
        _repo(branch_analyzer).head.is_detached = False
        _repo(branch_analyzer).active_branch.name = "feature/new-feature"

        branch_name = branch_analyzer.get_current_branch()

        assert branch_name == "feature/new-feature"

    def test_get_current_branch_main(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test getting current branch when on main."""
        _repo(branch_analyzer).head.is_detached = False
        _repo(branch_analyzer).active_branch.name = "main"

        branch_name = branch_analyzer.get_current_branch()

        assert branch_name == "main"

    def test_get_current_branch_detached_head(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test error when repository is in detached HEAD state."""
        _repo(branch_analyzer).head.is_detached = True

        with pytest.raises(BranchAnalyzerError, match="detached HEAD state"):
            branch_analyzer.get_current_branch()

    def test_get_current_branch_error(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test error handling when unable to get current branch."""
        type(branch_analyzer.repo.head).is_detached = PropertyMock(side_effect=Exception("Git error"))

        with pytest.raises(BranchAnalyzerError, match="Failed to get current branch"):
            branch_analyzer.get_current_branch()


class TestValidateBranchExists:
    """Tests for validate_branch_exists method."""

    def test_validate_local_branch_exists(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test validation of existing local branch."""
        mock_branch1 = MagicMock()
        mock_branch1.name = "main"
        mock_branch2 = MagicMock()
        mock_branch2.name = "develop"

        _repo(branch_analyzer).branches = [mock_branch1, mock_branch2]

        assert branch_analyzer.validate_branch_exists("main") is True
        assert branch_analyzer.validate_branch_exists("develop") is True

    def test_validate_local_branch_not_exists(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test validation of non-existent local branch."""
        mock_branch = MagicMock()
        mock_branch.name = "main"
        _repo(branch_analyzer).branches = [mock_branch]

        assert branch_analyzer.validate_branch_exists("nonexistent") is False

    def test_validate_remote_branch_exists(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test validation of remote branch reference (e.g., origin/main)."""
        # Mock remote refs
        mock_ref1 = MagicMock()
        mock_ref1.name = "origin/main"
        mock_ref2 = MagicMock()
        mock_ref2.name = "origin/develop"

        mock_remote = MagicMock()
        mock_remote.refs = [mock_ref1, mock_ref2]

        _repo(branch_analyzer).remote.return_value = mock_remote

        assert branch_analyzer.validate_branch_exists("origin/main") is True
        assert branch_analyzer.validate_branch_exists("origin/develop") is True

    def test_validate_remote_branch_not_exists(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test validation of non-existent remote branch."""
        mock_ref = MagicMock()
        mock_ref.name = "origin/main"

        mock_remote = MagicMock()
        mock_remote.refs = [mock_ref]

        _repo(branch_analyzer).remote.return_value = mock_remote

        assert branch_analyzer.validate_branch_exists("origin/nonexistent") is False

    def test_validate_branch_checks_default_remote(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test that validation checks default remote for simple branch names."""
        # No local branches
        _repo(branch_analyzer).branches = []

        # Mock default remote (origin)
        mock_ref = MagicMock()
        mock_ref.name = "origin/main"

        mock_remote = MagicMock()
        mock_remote.refs = [mock_ref]

        _repo(branch_analyzer).remote.return_value = mock_remote

        # Should find 'main' in origin/main
        assert branch_analyzer.validate_branch_exists("main") is True

    def test_validate_branch_exception_handling(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test that exceptions during validation return False."""
        _repo(branch_analyzer).branches = []
        _repo(branch_analyzer).remote.side_effect = Exception("Remote error")

        # Should return False instead of raising
        assert branch_analyzer.validate_branch_exists("main") is False

    def test_validate_remote_branch_invalid_remote(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test validation when remote doesn't exist."""
        _repo(branch_analyzer).remote.side_effect = Exception("Remote not found")

        assert branch_analyzer.validate_branch_exists("upstream/main") is False


class TestGetUserEmail:
    """Tests for get_user_email method."""

    def test_get_user_email_success(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test getting user email from git config."""
        mock_config = MagicMock()
        mock_config.get_value.return_value = "user@example.com"
        _repo(branch_analyzer).config_reader.return_value = mock_config

        email = branch_analyzer.get_user_email()

        assert email == "user@example.com"
        mock_config.get_value.assert_called_once_with("user", "email", default=None)

    def test_get_user_email_not_configured(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test error when user email is not configured."""
        mock_config = MagicMock()
        mock_config.get_value.return_value = None
        _repo(branch_analyzer).config_reader.return_value = mock_config

        with pytest.raises(BranchAnalyzerError, match="Git user email not configured"):
            branch_analyzer.get_user_email()

    def test_get_user_email_config_error(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test error handling when reading config fails."""
        _repo(branch_analyzer).config_reader.side_effect = Exception("Config read error")

        with pytest.raises(BranchAnalyzerError, match="Failed to get user email"):
            branch_analyzer.get_user_email()

    def test_get_user_email_various_formats(self, branch_analyzer: BranchAnalyzer) -> None:
        """Test that various email formats are accepted."""
        mock_config = MagicMock()
        _repo(branch_analyzer).config_reader.return_value = mock_config

        test_emails = [
            "simple@example.com",
            "user+tag@example.com",
            "first.last@example.co.uk",
            "123@example.com",
        ]

        for test_email in test_emails:
            mock_config.get_value.return_value = test_email
            email = branch_analyzer.get_user_email()
            assert email == test_email
