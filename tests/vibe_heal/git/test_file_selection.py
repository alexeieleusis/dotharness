"""Tests for the shared file-selection helpers (git/file_selection.py)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.file_selection import filter_files_by_patterns, select_modified_files


@pytest.fixture
def mock_branch_analyzer() -> MagicMock:
    """Create a mocked BranchAnalyzer."""
    return MagicMock(spec=BranchAnalyzer)


class TestFilterFilesByPatterns:
    """Tests for filter_files_by_patterns."""

    def test_filter_single_pattern(self) -> None:
        """Test filtering with a single pattern."""
        files = [
            Path("src/file1.py"),
            Path("src/file2.ts"),
            Path("test/test1.py"),
        ]

        result = filter_files_by_patterns(files, ["*.py"])

        assert len(result) == 2
        assert Path("src/file1.py") in result
        assert Path("test/test1.py") in result

    def test_filter_multiple_patterns(self) -> None:
        """Test filtering with multiple patterns."""
        files = [
            Path("src/file1.py"),
            Path("src/file2.ts"),
            Path("src/file3.js"),
            Path("test.txt"),
        ]

        result = filter_files_by_patterns(files, ["*.py", "*.ts"])

        assert len(result) == 2
        assert Path("src/file1.py") in result
        assert Path("src/file2.ts") in result

    def test_filter_glob_pattern_uses_path_match(self) -> None:
        """Test filtering with glob patterns (Path.match, not fnmatch)."""
        files = [
            Path("src/module/file1.py"),
            Path("src/file2.py"),
            Path("test/test1.py"),
        ]

        result = filter_files_by_patterns(files, ["src/**/*.py"])

        # Path.match only matches the nested module file
        assert len(result) == 1
        assert Path("src/module/file1.py") in result

    def test_filter_no_matches(self) -> None:
        """Test filtering when no files match."""
        files = [
            Path("src/file1.py"),
            Path("src/file2.py"),
        ]

        result = filter_files_by_patterns(files, ["*.ts"])

        assert len(result) == 0

    def test_filter_all_match(self) -> None:
        """Test filtering when all files match."""
        files = [
            Path("file1.py"),
            Path("file2.py"),
            Path("file3.py"),
        ]

        result = filter_files_by_patterns(files, ["*.py"])

        assert len(result) == 3


class TestSelectModifiedFiles:
    """Tests for select_modified_files."""

    def test_returns_modified_files(self, mock_branch_analyzer: MagicMock) -> None:
        """Returns the analyzer's modified files when no patterns are given."""
        files = [Path("src/a.py"), Path("src/b.ts")]
        mock_branch_analyzer.get_modified_files.return_value = files

        result = select_modified_files(mock_branch_analyzer, "origin/main")

        assert result == files
        mock_branch_analyzer.get_modified_files.assert_called_once_with("origin/main")

    def test_returns_empty_when_no_modified_files(self, mock_branch_analyzer: MagicMock) -> None:
        """Returns an empty list when the branch has no modified files."""
        mock_branch_analyzer.get_modified_files.return_value = []

        result = select_modified_files(mock_branch_analyzer, "origin/main")

        assert result == []

    def test_filters_by_patterns(self, mock_branch_analyzer: MagicMock) -> None:
        """Patterns are applied with Path.match semantics."""
        files = [Path("src/module/a.py"), Path("src/b.py"), Path("data/c.json")]
        mock_branch_analyzer.get_modified_files.return_value = files

        result = select_modified_files(mock_branch_analyzer, "origin/main", ["src/**/*.py"])

        assert result == [Path("src/module/a.py")]

    def test_patterns_excluding_all_files(self, mock_branch_analyzer: MagicMock) -> None:
        """An empty result is returned when patterns exclude every file."""
        mock_branch_analyzer.get_modified_files.return_value = [Path("src/a.py")]

        result = select_modified_files(mock_branch_analyzer, "origin/main", ["*.ts"])

        assert result == []
