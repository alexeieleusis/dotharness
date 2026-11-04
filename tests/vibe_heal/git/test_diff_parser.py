"""Tests for DiffParser class."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from git import GitCommandError, Repo

from dotharness.vibe_heal.git.diff_parser import DiffLines, DiffParser, DiffParserError

DIFF_SIMPLE_ADD = """\
diff --git a/src/example.py b/src/example.py
index 1234567..abcdefg 100644
--- a/src/example.py
+++ b/src/example.py
@@ -5 +5,3 @@
+new line one
+new line two
+new line three
"""

DIFF_PURE_DELETION = """\
diff --git a/src/example.py b/src/example.py
index 1234567..abcdefg 100644
--- a/src/example.py
+++ b/src/example.py
@@ -3,2 +3 @@
-deleted line one
-deleted line two
"""

DIFF_MODIFICATION = """\
diff --git a/src/example.py b/src/example.py
index 1234567..abcdefg 100644
--- a/src/example.py
+++ b/src/example.py
@@ -3 +3 @@
-old line
+new line
"""

DIFF_MULTIPLE_HUNKS = """\
diff --git a/src/example.py b/src/example.py
index 1234567..abcdefg 100644
--- a/src/example.py
+++ b/src/example.py
@@ -3 +3 @@
-old
+new
@@ -10,0 +11 @@
+inserted line
"""

DIFF_MULTIPLE_FILES = """\
diff --git a/src/file1.py b/src/file1.py
index 1234567..abcdefg 100644
--- a/src/file1.py
+++ b/src/file1.py
@@ -5,0 +5 @@
+added in file1
diff --git a/src/file2.py b/src/file2.py
index 1111111..2222222 100644
--- a/src/file2.py
+++ b/src/file2.py
@@ -10 +10 @@
-old in file2
+new in file2
"""

DIFF_EMPTY = ""

DIFF_BINARY = """\
diff --git a/image.png b/image.png
new file mode 100644
Binary files /dev/null and b/image.png differ
"""

DIFF_RENAMED_FILE = """\
diff --git a/src/old_name.py b/src/new_name.py
similarity index 90%
rename from src/old_name.py
rename to src/new_name.py
index 1234567..abcdefg 100644
--- a/src/old_name.py
+++ b/src/new_name.py
@@ -5 +5 @@
-old content
+new content
"""

DIFF_MIXED_HUNKS = """\
diff --git a/src/example.py b/src/example.py
index 1234567..abcdefg 100644
--- a/src/example.py
+++ b/src/example.py
@@ -3,0 +3 @@
+added at 3
@@ -7 +6 @@
-deleted at 7
@@ -12 +10,0 @@
+added at 10
"""


@pytest.fixture
def mock_repo(tmp_path: Path) -> MagicMock:
    """Create a mock Git repository."""
    repo = MagicMock(spec=Repo)
    repo.working_dir = str(tmp_path)
    return repo


@pytest.fixture
def parser(tmp_path: Path, mock_repo: MagicMock) -> DiffParser:
    """Create a DiffParser instance with mocked repo."""
    with patch("dotharness.vibe_heal.git.diff_parser.Repo", return_value=mock_repo):
        p = DiffParser(tmp_path)
    return p


class TestDiffParserInit:
    """Tests for DiffParser initialization."""

    def test_init_valid_repo(self, tmp_path: Path, mock_repo: MagicMock) -> None:
        """Test initialization with valid repository."""
        with patch("dotharness.vibe_heal.git.diff_parser.Repo", return_value=mock_repo):
            p = DiffParser(tmp_path)

        assert p.repo == mock_repo

    def test_init_invalid_repo(self, tmp_path: Path) -> None:
        """Test initialization with invalid repository raises error."""
        from git.exc import InvalidGitRepositoryError

        with (
            patch(
                "dotharness.vibe_heal.git.diff_parser.Repo",
                side_effect=InvalidGitRepositoryError("Not a git repo"),
            ),
            pytest.raises(DiffParserError, match="Not a valid git repository"),
        ):
            DiffParser(tmp_path)


class TestParseChangedLines:
    """Tests for get_changed_lines method."""

    def test_simple_addition(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing a simple addition hunk."""
        mock_repo.git.diff.return_value = DIFF_SIMPLE_ADD

        result = parser.get_changed_lines("origin/main")

        # Added lines 5-7 plus 3-line trailing context (8-10)
        assert result == {"src/example.py": {5, 6, 7, 8, 9, 10}}

    def test_pure_deletion(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing a pure deletion produces empty set."""
        mock_repo.git.diff.return_value = DIFF_PURE_DELETION

        result = parser.get_changed_lines("origin/main")

        # Deletions produce no new-file lines, so no trailing context either
        assert result == {"src/example.py": set()}

    def test_modification(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing a modification (del+add) hunk."""
        mock_repo.git.diff.return_value = DIFF_MODIFICATION

        result = parser.get_changed_lines("origin/main")

        # Modified line 3 plus 3-line trailing context (4-6)
        assert result == {"src/example.py": {3, 4, 5, 6}}

    def test_multiple_hunks(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing multiple hunks in the same file."""
        mock_repo.git.diff.return_value = DIFF_MULTIPLE_HUNKS

        result = parser.get_changed_lines("origin/main")

        # Hunk 1 adds line 3 + trailing 4-6; hunk 2 adds line 11 + trailing 12-14
        assert result == {"src/example.py": {3, 4, 5, 6, 11, 12, 13, 14}}

    def test_multiple_files(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing changes across multiple files."""
        mock_repo.git.diff.return_value = DIFF_MULTIPLE_FILES

        result = parser.get_changed_lines("origin/main")

        assert result == {
            "src/file1.py": {5, 6, 7, 8},
            "src/file2.py": {10, 11, 12, 13},
        }

    def test_empty_diff(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing an empty diff returns empty dict."""
        mock_repo.git.diff.return_value = DIFF_EMPTY

        result = parser.get_changed_lines("origin/main")

        assert result == {}

    def test_binary_file_ignored(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test that binary file diffs are ignored."""
        mock_repo.git.diff.return_value = DIFF_BINARY

        result = parser.get_changed_lines("origin/main")

        assert result == {}

    def test_renamed_file(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test parsing a renamed file uses the new name."""
        mock_repo.git.diff.return_value = DIFF_RENAMED_FILE

        result = parser.get_changed_lines("origin/main")

        assert result == {"src/new_name.py": {5, 6, 7, 8}}

    def test_mixed_hunks(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test file with addition, deletion-only, and another addition hunk."""
        mock_repo.git.diff.return_value = DIFF_MIXED_HUNKS

        result = parser.get_changed_lines("origin/main")

        # Hunk 1 adds line 3 + trailing 4-6; hunk 3 adds line 10 + trailing 11-13
        assert result == {"src/example.py": {3, 4, 5, 6, 10, 11, 12, 13}}

    def test_uses_merge_base_diff(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test that merge-base SHA is resolved before diffing."""
        mock_repo.git.merge_base.return_value = "abc123sha"
        mock_repo.git.diff.return_value = ""

        parser.get_changed_lines("origin/main")

        mock_repo.git.merge_base.assert_called_once_with("origin/main", "HEAD")
        mock_repo.git.diff.assert_called_once_with("--no-color", "--unified=0", "abc123sha..HEAD")

    def test_custom_base_branch(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test with custom base branch."""
        mock_repo.git.merge_base.return_value = "deadbeef"
        mock_repo.git.diff.return_value = ""

        parser.get_changed_lines("develop")

        mock_repo.git.merge_base.assert_called_once_with("develop", "HEAD")
        mock_repo.git.diff.assert_called_once_with("--no-color", "--unified=0", "deadbeef..HEAD")

    def test_git_command_error(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test error handling when git diff fails."""
        mock_repo.git.merge_base.return_value = "abc123sha"
        mock_repo.git.diff.side_effect = GitCommandError("git diff", 1)

        with pytest.raises(DiffParserError, match="Git diff command failed"):
            parser.get_changed_lines("origin/main")

    def test_context_lines_increment_counter(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test that context lines (space-prefixed) increment the new_line counter."""
        diff_with_context = """\
diff --git a/src/example.py b/src/example.py
index 1234567..abcdefg 100644
--- a/src/example.py
+++ b/src/example.py
@@ -5 +5 @@
 context line
+new line
"""
        mock_repo.git.diff.return_value = diff_with_context

        result = parser.get_changed_lines("origin/main")

        # Added line 6 plus 3-line trailing context (7-9)
        assert result == {"src/example.py": {6, 7, 8, 9}}

    def test_trailing_context_catches_adjacent_unchanged_line(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Trailing context window includes lines immediately after an insertion.

        Regression: SonarQube reports nested-ternary violations on the first
        unchanged line after an insertion (e.g. issue at line 287 when lines
        285-286 were inserted). Without trailing context the line filter drops
        the issue even though the PR introduced it.
        """
        diff_insertion = """\
diff --git a/src/osc-edge.tsx b/src/osc-edge.tsx
index 1234567..abcdefg 100644
--- a/src/osc-edge.tsx
+++ b/src/osc-edge.tsx
@@ -282,0 +285,2 @@
+                : isNotNil(existsInTrail) && !existsInTrail
+                  ? color.neutral['200']
"""
        mock_repo.git.diff.return_value = diff_insertion

        result = parser.get_changed_lines("origin/main")

        # Inserted lines 285-286; trailing context must include 287 (and 288, 289)
        assert "src/osc-edge.tsx" in result
        assert 285 in result["src/osc-edge.tsx"]
        assert 286 in result["src/osc-edge.tsx"]
        assert 287 in result["src/osc-edge.tsx"]

    def test_no_hunks_in_diff(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Test diff with file header but no @@ hunk headers."""
        diff_no_hunks = """\
diff --git a/src/example.py b/src/example.py
new file mode 100644
"""
        mock_repo.git.diff.return_value = diff_no_hunks

        result = parser.get_changed_lines("origin/main")

        assert result == {}


class TestGetDiffLines:
    """Tests for get_diff_lines method and old-line tracking."""

    def test_returns_diff_lines_dataclass(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """get_diff_lines returns a DiffLines with new_lines and old_lines."""
        mock_repo.git.diff.return_value = DIFF_MODIFICATION

        result = parser.get_diff_lines("origin/main")

        assert isinstance(result, DiffLines)
        assert isinstance(result.new_lines, dict)
        assert isinstance(result.old_lines, dict)

    def test_pure_deletion_populates_old_lines(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """A pure deletion hunk adds the removed lines to old_lines."""
        mock_repo.git.diff.return_value = DIFF_PURE_DELETION

        result = parser.get_diff_lines("origin/main")

        assert result.old_lines == {"src/example.py": {3, 4}}
        assert result.new_lines == {"src/example.py": set()}

    def test_modification_appears_in_both_sides(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """A modification puts the old line in old_lines and new line in new_lines."""
        mock_repo.git.diff.return_value = DIFF_MODIFICATION

        result = parser.get_diff_lines("origin/main")

        assert 3 in result.old_lines["src/example.py"]
        assert 3 in result.new_lines["src/example.py"]

    def test_pure_addition_leaves_old_lines_empty(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """A pure insertion has no old-side lines."""
        mock_repo.git.diff.return_value = DIFF_SIMPLE_ADD

        result = parser.get_diff_lines("origin/main")

        assert result.old_lines == {"src/example.py": set()}

    def test_multiple_hunks_accumulate_old_lines(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Old lines from multiple hunks are all collected."""
        mock_repo.git.diff.return_value = DIFF_MULTIPLE_HUNKS

        result = parser.get_diff_lines("origin/main")

        # Hunk 1: -3 (modification), hunk 2: pure insertion at +11 (no old lines)
        assert result.old_lines == {"src/example.py": {3}}

    def test_old_lines_not_expanded_with_trailing_context(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Trailing-context window is applied to new_lines but NOT to old_lines."""
        mock_repo.git.diff.return_value = DIFF_MODIFICATION

        result = parser.get_diff_lines("origin/main")

        # new_lines gets trailing context (3→6); old_lines stays exact
        assert result.new_lines["src/example.py"] == {3, 4, 5, 6}
        assert result.old_lines["src/example.py"] == {3}

    def test_empty_diff_returns_empty_diff_lines(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """Empty diff produces DiffLines with empty dicts."""
        mock_repo.git.diff.return_value = DIFF_EMPTY

        result = parser.get_diff_lines("origin/main")

        assert result.new_lines == {}
        assert result.old_lines == {}

    def test_get_changed_lines_still_works(self, parser: DiffParser, mock_repo: MagicMock) -> None:
        """get_changed_lines() is a backwards-compatible wrapper for new_lines."""
        mock_repo.git.diff.return_value = DIFF_MODIFICATION

        changed = parser.get_changed_lines("origin/main")
        diff = parser.get_diff_lines("origin/main")

        assert changed == diff.new_lines
