"""Tests for Git manager."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import git
import pytest

from dotharness.subprocess_runner import RunResult
from dotharness.vibe_heal.ai_tools.base import AIToolType
from dotharness.vibe_heal.git import (
    DirtyWorkingDirectoryError,
    GitManager,
    GitOperationError,
    NotAGitRepositoryError,
)
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a temporary Git repository.

    Args:
        tmp_path: Pytest temporary directory fixture

    Returns:
        Path to the temporary Git repository
    """
    repo = git.Repo.init(tmp_path)

    # Create initial commit
    test_file = tmp_path / "test.py"
    test_file.write_text("print('hello')")
    repo.index.add(["test.py"])
    repo.index.commit("Initial commit")

    return tmp_path


@pytest.fixture
def sample_issue() -> SonarQubeIssue:
    """Create a sample issue for testing.

    Returns:
        Sample SonarQube issue
    """
    return SonarQubeIssue(
        key="ABC123",
        rule="python:S1481",
        severity="MAJOR",
        message="Remove unused import",
        component="project:test.py",
        line=10,
        status="OPEN",
        type="CODE_SMELL",
    )


class TestGitManagerInit:
    """Tests for GitManager initialization."""

    def test_init_with_git_repo(self, git_repo: Path) -> None:
        """Test initialization in a Git repository."""
        manager = GitManager(git_repo)

        assert manager.repo_path == git_repo
        assert manager.is_repository()

    def test_init_with_current_directory(self, git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test initialization with current directory."""
        monkeypatch.chdir(git_repo)

        manager = GitManager()

        assert manager.is_repository()

    def test_init_with_non_git_directory(self, tmp_path: Path) -> None:
        """Test initialization fails for non-Git directory."""
        non_git_dir = tmp_path / "not_a_repo"
        non_git_dir.mkdir()

        with pytest.raises(NotAGitRepositoryError, match="Not a Git repository"):
            GitManager(non_git_dir)


class TestGitManagerRepository:
    """Tests for repository checks."""

    def test_is_repository_in_git_repo(self, git_repo: Path) -> None:
        """Test is_repository returns True in Git repo."""
        manager = GitManager(git_repo)

        assert manager.is_repository() is True

    def test_get_current_branch(self, git_repo: Path) -> None:
        """Test getting current branch name."""
        manager = GitManager(git_repo)

        # Git repos default to 'master' or 'main'
        branch = manager.get_current_branch()
        assert branch in ["master", "main"]

    def test_get_current_branch_custom_name(self, git_repo: Path) -> None:
        """Test getting current branch with custom name."""
        manager = GitManager(git_repo)

        # Create and checkout a new branch
        manager.repo.create_head("feature-branch")
        manager.repo.heads["feature-branch"].checkout()

        assert manager.get_current_branch() == "feature-branch"


class TestGitManagerCleanState:
    """Tests for clean state checks."""

    def test_is_clean_with_clean_repo(self, git_repo: Path) -> None:
        """Test is_clean returns True for clean repo."""
        manager = GitManager(git_repo)

        assert manager.is_clean() is True

    def test_is_clean_with_modified_file(self, git_repo: Path) -> None:
        """Test is_clean returns False with modified file."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        assert manager.is_clean() is False

    def test_is_clean_with_untracked_file(self, git_repo: Path) -> None:
        """Test is_clean returns False with untracked file."""
        manager = GitManager(git_repo)

        # Create untracked file
        (git_repo / "new_file.py").write_text("print('new')")

        assert manager.is_clean() is False

    def test_is_clean_with_staged_file(self, git_repo: Path) -> None:
        """Test is_clean returns False with staged file."""
        manager = GitManager(git_repo)

        # Modify and stage file
        (git_repo / "test.py").write_text("print('staged')")
        manager.repo.index.add(["test.py"])

        assert manager.is_clean() is False

    def test_get_uncommitted_files_empty(self, git_repo: Path) -> None:
        """Test get_uncommitted_files returns empty list for clean repo."""
        manager = GitManager(git_repo)

        uncommitted = manager.get_uncommitted_files()

        assert uncommitted == []

    def test_get_uncommitted_files_with_changes(self, git_repo: Path) -> None:
        """Test get_uncommitted_files returns list of changed files."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        # Create untracked file
        (git_repo / "new_file.py").write_text("print('new')")

        uncommitted = manager.get_uncommitted_files()

        assert "test.py" in uncommitted
        assert "new_file.py" in uncommitted


class TestGitManagerCommit:
    """Tests for commit operations."""

    async def test_commit_fix_creates_commit(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix creates a commit."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('fixed')")

        # Commit fix
        sha = await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE)

        assert sha
        assert len(sha) == 40  # SHA-1 hex length
        assert manager.is_clean()

    async def test_commit_fix_message_format(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix creates commit with correct message."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('fixed')")

        # Commit fix (without rule details)
        await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE, rule=None)

        # Check commit message (should use old format without rule)
        commit = manager.repo.head.commit
        assert "[SQ-S1481]" in commit.message
        assert "Remove unused import" in commit.message
        assert "python:S1481" in commit.message
        assert "MAJOR" in commit.message
        assert "Claude Code" in commit.message

    async def test_commit_fix_message_truncation(self, git_repo: Path) -> None:
        """Test commit_fix truncates long messages."""
        manager = GitManager(git_repo)

        # Create issue with long message
        long_message = "A" * 100
        issue = SonarQubeIssue(
            key="ABC123",
            rule="python:S1481",
            severity="MAJOR",
            message=long_message,
            component="project:test.py",
            line=10,
            status="OPEN",
            type="CODE_SMELL",
        )

        # Modify file
        (git_repo / "test.py").write_text("print('fixed')")

        # Commit fix
        await manager.commit_fix(issue, ["test.py"], AIToolType.CLAUDE_CODE)

        # Check commit message subject line
        commit = manager.repo.head.commit
        subject_line = commit.message.split("\n")[0]

        # Subject should be truncated with "..."
        assert len(subject_line) < len(long_message) + 20  # +20 for prefix
        assert "..." in subject_line

    async def test_commit_fix_stages_files(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix stages specified files."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('fixed')")

        # Commit fix
        await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE)

        # File should be in the commit
        commit = manager.repo.head.commit
        assert "test.py" in commit.stats.files

    async def test_commit_fix_multiple_files(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix handles multiple files."""
        manager = GitManager(git_repo)

        # Create and modify multiple files
        (git_repo / "test.py").write_text("print('fixed 1')")
        file2 = git_repo / "test2.py"
        file2.write_text("print('fixed 2')")

        # Commit fix with multiple files
        await manager.commit_fix(sample_issue, ["test.py", "test2.py"], AIToolType.CLAUDE_CODE)

        # Both files should be in the commit
        commit = manager.repo.head.commit
        assert "test.py" in commit.stats.files
        assert "test2.py" in commit.stats.files

    async def test_commit_fix_raises_with_no_files(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix raises error with no files."""
        manager = GitManager(git_repo)

        with pytest.raises(GitOperationError, match="No files to commit"):
            await manager.commit_fix(sample_issue, [], AIToolType.CLAUDE_CODE)

    async def test_commit_fix_returns_sha(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix returns commit SHA."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('fixed')")

        # Commit fix
        sha = await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE)

        # Verify SHA matches actual commit
        assert sha == manager.repo.head.commit.hexsha

    async def test_commit_fix_returns_none_when_no_changes(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix returns None when there are no changes to commit."""
        manager = GitManager(git_repo)

        # Don't modify any files - try to commit the same file that's already in the repo
        # This simulates the case where a previous fix already resolved this issue

        # Commit fix - should return None because there are no changes
        sha = await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE)

        # Should return None when no changes
        assert sha is None

    async def test_commit_fix_no_empty_commit_created(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix does not create an empty commit."""
        manager = GitManager(git_repo)

        # Get current HEAD
        old_head = manager.repo.head.commit.hexsha

        # Try to commit without any changes
        sha = await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE)

        # Should return None
        assert sha is None

        # HEAD should not have moved
        assert manager.repo.head.commit.hexsha == old_head

        # Repository should still be clean
        assert manager.is_clean()

    async def test_commit_fix_auto_detects_modified_files(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix auto-detects all modified files when files=None."""
        manager = GitManager(git_repo)

        # Create and commit a second file first
        file2 = git_repo / "test2.py"
        file2.write_text("print('original')")
        manager.repo.index.add(["test2.py"])
        manager.repo.index.commit("Add test2.py")

        # Now modify both tracked files
        (git_repo / "test.py").write_text("print('fixed 1')")
        file2.write_text("print('fixed 2')")

        # Commit fix with files=None to auto-detect
        sha = await manager.commit_fix(sample_issue, None, AIToolType.CLAUDE_CODE)

        # Should create commit
        assert sha is not None

        # Both files should be in the commit
        commit = manager.repo.head.commit
        assert "test.py" in commit.stats.files
        assert "test2.py" in commit.stats.files

    async def test_commit_fix_auto_detect_excludes_untracked(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix auto-detect excludes untracked files."""
        manager = GitManager(git_repo)

        # Modify tracked file
        (git_repo / "test.py").write_text("print('fixed')")

        # Create untracked file
        (git_repo / "new_file.py").write_text("print('new')")

        # Commit fix with files=None to auto-detect
        sha = await manager.commit_fix(sample_issue, None, AIToolType.CLAUDE_CODE)

        # Should create commit
        assert sha is not None

        # Only tracked file should be in commit
        commit = manager.repo.head.commit
        assert "test.py" in commit.stats.files
        assert "new_file.py" not in commit.stats.files

    async def test_commit_fix_message_includes_file_count(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix includes file count in message for multiple files."""
        manager = GitManager(git_repo)

        # Modify multiple files
        (git_repo / "test.py").write_text("print('fixed 1')")
        file2 = git_repo / "test2.py"
        file2.write_text("print('fixed 2')")
        file3 = git_repo / "test3.py"
        file3.write_text("print('fixed 3')")

        # Commit fix with multiple files
        await manager.commit_fix(sample_issue, ["test.py", "test2.py", "test3.py"], AIToolType.CLAUDE_CODE)

        # Check commit message includes file count
        commit = manager.repo.head.commit
        assert "Files modified: 3" in commit.message

    async def test_commit_fix_message_no_file_count_for_single_file(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test commit_fix does not include file count for single file."""
        manager = GitManager(git_repo)

        # Modify single file
        (git_repo / "test.py").write_text("print('fixed')")

        # Commit fix with single file
        await manager.commit_fix(sample_issue, ["test.py"], AIToolType.CLAUDE_CODE)

        # Check commit message does not include file count
        commit = manager.repo.head.commit
        assert "Files modified:" not in commit.message


class TestGitManagerSafety:
    """Tests for safety checks."""

    def test_require_clean_working_directory_passes_when_clean(
        self,
        git_repo: Path,
    ) -> None:
        """Test require_clean_working_directory passes when clean."""
        manager = GitManager(git_repo)

        # Should not raise
        manager.require_clean_working_directory()

    def test_require_clean_working_directory_raises_when_dirty(
        self,
        git_repo: Path,
    ) -> None:
        """Test require_clean_working_directory raises when dirty."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        with pytest.raises(DirtyWorkingDirectoryError, match="uncommitted changes"):
            manager.require_clean_working_directory()

    def test_require_clean_working_directory_lists_files(
        self,
        git_repo: Path,
    ) -> None:
        """Test require_clean_working_directory lists uncommitted files."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        with pytest.raises(DirtyWorkingDirectoryError, match="test.py"):  # noqa: RUF043
            manager.require_clean_working_directory()

    def test_require_clean_working_directory_allows_untracked_files(
        self,
        git_repo: Path,
    ) -> None:
        """Test require_clean_working_directory allows untracked files."""
        manager = GitManager(git_repo)

        # Create untracked file
        (git_repo / "new_file.py").write_text("print('new')")

        # Should not raise - untracked files are OK
        manager.require_clean_working_directory()

    def test_require_clean_working_directory_error_message_mentions_untracked_ok(
        self,
        git_repo: Path,
    ) -> None:
        """Test error message mentions that untracked files are OK."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        with pytest.raises(DirtyWorkingDirectoryError, match="Untracked files are OK"):
            manager.require_clean_working_directory()


class TestGitManagerModifiedFiles:
    """Tests for modified/staged file detection."""

    def test_get_modified_or_staged_files_empty(self, git_repo: Path) -> None:
        """Test get_modified_or_staged_files returns empty list for clean repo."""
        manager = GitManager(git_repo)

        modified = manager.get_modified_or_staged_files()

        assert modified == []

    def test_get_modified_or_staged_files_with_modified(self, git_repo: Path) -> None:
        """Test get_modified_or_staged_files returns modified files."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        modified = manager.get_modified_or_staged_files()

        assert "test.py" in modified

    def test_get_modified_or_staged_files_with_staged(self, git_repo: Path) -> None:
        """Test get_modified_or_staged_files returns staged files."""
        manager = GitManager(git_repo)

        # Modify and stage file
        (git_repo / "test.py").write_text("print('staged')")
        manager.repo.index.add(["test.py"])

        modified = manager.get_modified_or_staged_files()

        assert "test.py" in modified

    def test_get_modified_or_staged_files_excludes_untracked(self, git_repo: Path) -> None:
        """Test get_modified_or_staged_files excludes untracked files."""
        manager = GitManager(git_repo)

        # Create untracked file
        (git_repo / "new_file.py").write_text("print('new')")

        modified = manager.get_modified_or_staged_files()

        assert "new_file.py" not in modified
        assert modified == []

    def test_has_modified_or_staged_files_false_for_clean(self, git_repo: Path) -> None:
        """Test has_modified_or_staged_files returns False for clean repo."""
        manager = GitManager(git_repo)

        assert manager.has_modified_or_staged_files() is False

    def test_has_modified_or_staged_files_true_for_modified(self, git_repo: Path) -> None:
        """Test has_modified_or_staged_files returns True with modified files."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        assert manager.has_modified_or_staged_files() is True

    def test_has_modified_or_staged_files_false_for_untracked(self, git_repo: Path) -> None:
        """Test has_modified_or_staged_files returns False with only untracked files."""
        manager = GitManager(git_repo)

        # Create untracked file
        (git_repo / "new_file.py").write_text("print('new')")

        assert manager.has_modified_or_staged_files() is False

    def test_get_all_modified_files(self, git_repo: Path) -> None:
        """Test get_all_modified_files returns all modified files."""
        manager = GitManager(git_repo)

        # Modify file
        (git_repo / "test.py").write_text("print('modified')")

        modified = manager.get_all_modified_files()

        assert "test.py" in modified


class TestPreCommitHooks:
    """Tests for pre-commit hook integration."""

    async def test_run_pre_commit_hooks_with_pre_commit_installed(self, git_repo: Path) -> None:
        """Test pre-commit CLI is used when available."""
        manager = GitManager(git_repo)

        with (
            patch("dotharness.vibe_heal.git.manager.shutil.which", return_value="/usr/bin/pre-commit") as mock_which,
            patch("dotharness.vibe_heal.git.manager.run") as mock_run,
        ):
            mock_run.return_value = RunResult(0, "", "")
            await manager._run_pre_commit_hooks(["test.py"])

        mock_which.assert_called_once_with("pre-commit")
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[:3] == ["pre-commit", "run", "--files"]
        assert "test.py" in call_args

    async def test_run_pre_commit_hooks_fallback_to_git_hook_run(self, git_repo: Path) -> None:
        """Test fallback to 'git hook run' when pre-commit not installed."""
        manager = GitManager(git_repo)

        with (
            patch("dotharness.vibe_heal.git.manager.shutil.which", return_value=None),
            patch("dotharness.vibe_heal.git.manager.run") as mock_run,
        ):
            mock_run.return_value = RunResult(0, "", "")
            await manager._run_pre_commit_hooks(["test.py"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args == ["git", "hook", "run", "--ignore-missing", "pre-commit"]

    async def test_run_pre_commit_hooks_disabled_when_empty_string(self, git_repo: Path) -> None:
        """Test hooks are skipped when pre_commit_command is empty string."""
        manager = GitManager(git_repo, pre_commit_command="")

        with patch("dotharness.vibe_heal.git.manager.run") as mock_run:
            result = await manager._run_pre_commit_hooks(["test.py"])

        mock_run.assert_not_called()
        assert result == []

    async def test_run_pre_commit_hooks_restages_modified_files(self, git_repo: Path) -> None:
        """Test that files modified by hooks are detected and returned for re-staging."""
        manager = GitManager(git_repo)

        # Stage the file first
        (git_repo / "test.py").write_text("print('modified')")
        manager.repo.index.add(["test.py"])

        def fake_run(cmd: list[str], **kwargs: object) -> RunResult:
            # Simulate hook modifying the file (different content from staged)
            (git_repo / "test.py").write_text("print('hook-fixed')")
            return RunResult(1, "", "")

        with (
            patch("dotharness.vibe_heal.git.manager.shutil.which", return_value=None),
            patch("dotharness.vibe_heal.git.manager.run", side_effect=fake_run),
        ):
            dirty = await manager._run_pre_commit_hooks(["test.py"])

        assert "test.py" in dirty

    async def test_custom_pre_commit_command_used_when_configured(self, git_repo: Path) -> None:
        """Test that a custom pre_commit_command is used when set."""
        manager = GitManager(git_repo, pre_commit_command="my-hook-runner --check")

        with patch("dotharness.vibe_heal.git.manager.run") as mock_run:
            mock_run.return_value = RunResult(0, "", "")
            await manager._run_pre_commit_hooks(["test.py"])

        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[:3] == ["my-hook-runner", "--check", "test.py"]

    async def test_hook_timeout_raises_git_operation_error(self, git_repo: Path) -> None:
        """A hanging hook is bounded by the timeout, runs without stdin, and raises GitOperationError."""
        manager = GitManager(git_repo, pre_commit_command="sleep 100000", pre_commit_timeout=7)
        timed_out = RunResult(returncode=124, stdout="", stderr="timed out after 7s", timed_out=True)

        with (
            patch("dotharness.vibe_heal.git.manager.run", return_value=timed_out) as mock_run,
            pytest.raises(GitOperationError, match="timed out after 7"),
        ):
            await manager._run_pre_commit_hooks(["test.py"])

        assert mock_run.call_args.kwargs["timeout"] == 7

    async def test_hook_not_found_raises_git_operation_error(self, git_repo: Path) -> None:
        """A missing hook binary is reported as GitOperationError."""
        manager = GitManager(git_repo, pre_commit_command="no-such-hook")
        missing = RunResult(returncode=127, stdout="", stderr="[Errno 2] no such file", not_found=True)

        with (
            patch("dotharness.vibe_heal.git.manager.run", return_value=missing),
            pytest.raises(GitOperationError, match="failed to run"),
        ):
            await manager._run_pre_commit_hooks(["test.py"])

    async def test_stage_and_commit_restages_hook_modified_files(
        self,
        git_repo: Path,
        sample_issue: SonarQubeIssue,
    ) -> None:
        """Test that _stage_and_commit re-stages files modified by hooks."""
        manager = GitManager(git_repo)

        (git_repo / "test.py").write_text("print('ai-fixed')")

        def fake_run(cmd: list[str], **kwargs: object) -> RunResult:
            # Simulate ruff-format changing the file after staging
            (git_repo / "test.py").write_text('print("ruff-formatted")\n')
            return RunResult(1, "", "")

        with (
            patch("dotharness.vibe_heal.git.manager.shutil.which", return_value=None),
            patch("dotharness.vibe_heal.git.manager.run", side_effect=fake_run),
        ):
            sha = await manager._stage_and_commit(["test.py"], "test commit")

        assert sha is not None
        commit = manager.repo.head.commit
        assert "test.py" in commit.stats.files
        # The committed content should be the hook-modified version
        blob = commit.tree["test.py"]
        assert b"ruff-formatted" in blob.data_stream.read()

    async def test_stage_and_commit_retries_on_hook_modified_files(self, git_repo: Path) -> None:
        """Test retry logic when commit fails due to dirty files."""
        manager = GitManager(git_repo, pre_commit_command="")

        (git_repo / "test.py").write_text("print('modified')")

        call_count = 0
        original_do_commit = manager._do_commit

        def failing_then_succeeding(msg: str) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Simulate hook modifying file during commit
                (git_repo / "test.py").write_text('print("retry-version")\n')
                raise git.GitError("pre-commit hook failed")  # noqa: TRY003
            return original_do_commit(msg)

        with patch.object(manager, "_do_commit", side_effect=failing_then_succeeding):
            sha = await manager._stage_and_commit(["test.py"], "retry test")

        assert sha is not None
        assert call_count == 2

    async def test_stage_and_commit_raises_after_two_failures(self, git_repo: Path) -> None:
        """Test GitOperationError raised when both commit attempts fail."""
        manager = GitManager(git_repo, pre_commit_command="")

        (git_repo / "test.py").write_text("print('modified')")
        manager.repo.index.add(["test.py"])

        with (
            patch.object(manager, "_do_commit", side_effect=git.GitError("persistent hook failure")),
            pytest.raises(GitOperationError, match="Failed to create commit"),
        ):
            await manager._stage_and_commit(["test.py"], "will fail")


class TestStageAndCommitDeletedFiles:
    """Tests for committing deleted tracked files."""

    async def test_commit_deleted_tracked_file(self, git_repo: Path) -> None:
        """A deleted tracked file is staged as a removal instead of raising."""
        manager = GitManager(git_repo, pre_commit_command="")
        (git_repo / "test.py").unlink()

        sha = await manager.create_commit("remove test.py")

        assert sha is not None
        assert "test.py" not in [b.path for b in manager.repo.head.commit.tree.traverse()]

    async def test_commit_fix_auto_detect_deleted_file(self, git_repo: Path, sample_issue: SonarQubeIssue) -> None:
        """commit_fix with files=None handles a deleted tracked file."""
        manager = GitManager(git_repo, pre_commit_command="")
        (git_repo / "test.py").unlink()

        sha = await manager.commit_fix(sample_issue, None, AIToolType.CLAUDE_CODE)

        assert sha is not None

    async def test_oserror_wrapped_in_git_operation_error(self, git_repo: Path) -> None:
        """OSError during staging is wrapped in GitOperationError."""
        manager = GitManager(git_repo, pre_commit_command="")
        (git_repo / "test.py").write_text("x")

        mock_index = MagicMock()
        mock_index.add.side_effect = OSError("boom")
        manager.repo = MagicMock(working_dir=str(git_repo), index=mock_index)

        with pytest.raises(GitOperationError, match="Failed to create commit"):
            await manager._stage_and_commit(["test.py"], "msg")
