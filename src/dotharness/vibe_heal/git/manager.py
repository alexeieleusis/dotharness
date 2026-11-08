"""Git operations manager."""

import shlex
import shutil
from pathlib import Path

import git

from dotharness.subprocess_runner import run
from dotharness.vibe_heal.ai_tools.base import AIToolType
from dotharness.vibe_heal.git.exceptions import (
    DirtyWorkingDirectoryError,
    GitError,
    GitOperationError,
    NotAGitRepositoryError,
)
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue, SonarQubeRule


class GitManager:
    """Manages Git operations for vibe_heal."""

    def __init__(
        self,
        repo_path: str | Path | None = None,
        pre_commit_command: str | None = None,
        pre_commit_timeout: float = 300,
    ) -> None:
        """Initialize Git manager.

        Args:
            repo_path: Path to Git repository (default: current directory)
            pre_commit_command: Command to run before committing. None means auto-detect,
                empty string means disabled.
            pre_commit_timeout: Max seconds to wait for the hook subprocess before killing it

        Raises:
            NotAGitRepositoryError: If path is not a Git repository
        """
        self.repo_path = Path(repo_path or Path.cwd())
        self._pre_commit_command = pre_commit_command
        self._pre_commit_timeout = pre_commit_timeout

        try:
            self.repo = git.Repo(self.repo_path, search_parent_directories=True)
        except git.InvalidGitRepositoryError as e:
            msg = f"Not a Git repository: {self.repo_path}"
            raise NotAGitRepositoryError(msg) from e
        except git.GitError as e:
            msg = f"Git error: {e}"
            raise GitError(msg) from e

    def is_repository(self) -> bool:
        """Check if current directory is a Git repository.

        Returns:
            True if in a Git repository
        """
        try:
            return self.repo.git_dir is not None
        except Exception:
            return False

    def is_clean(self) -> bool:
        """Check if working directory is clean (no uncommitted changes).

        Returns:
            True if working directory is clean
        """
        return not self.repo.is_dirty(untracked_files=True)

    def get_current_branch(self) -> str:
        """Get the name of the current branch.

        Returns:
            Current branch name

        Raises:
            GitOperationError: If unable to determine branch
        """
        try:
            return self.repo.active_branch.name
        except Exception as e:
            msg = f"Unable to get current branch: {e}"
            raise GitOperationError(msg) from e

    def get_uncommitted_files(self) -> list[str]:
        """Get list of uncommitted files.

        Returns:
            List of file paths with uncommitted changes
        """
        changed = [item.a_path for item in self.repo.index.diff(None) if item.a_path is not None]
        staged = [item.a_path for item in self.repo.index.diff("HEAD") if item.a_path is not None]
        untracked = self.repo.untracked_files

        return list(set(changed + staged + untracked))

    def get_modified_or_staged_files(self) -> list[str]:
        """Get list of modified or staged files (excluding untracked files).

        Returns:
            List of file paths that are modified or staged (but not untracked)
        """
        changed = [item.a_path for item in self.repo.index.diff(None) if item.a_path is not None]
        staged = [item.a_path for item in self.repo.index.diff("HEAD") if item.a_path is not None]

        return list(set(changed + staged))

    def has_modified_or_staged_files(self) -> bool:
        """Check if there are any modified or staged files (excluding untracked).

        Returns:
            True if there are modified or staged files
        """
        return len(self.get_modified_or_staged_files()) > 0

    def get_all_modified_files(self) -> list[str]:
        """Get all files that have been modified in the working directory.

        This includes both staged and unstaged changes, but excludes untracked files.

        Returns:
            List of file paths with modifications
        """
        return self.get_modified_or_staged_files()

    def has_uncommitted_changes(self, file_path: str) -> bool:
        """Check if a specific file has uncommitted changes.

        Args:
            file_path: Path to file to check

        Returns:
            True if file has uncommitted changes or is staged
        """
        # Normalize path to match git's format
        file_path_normalized = str(Path(file_path))

        # Check if file is in changed files (working directory changes)
        changed = [item.a_path for item in self.repo.index.diff(None)]
        if file_path_normalized in changed:
            return True

        # Check if file is staged
        staged = [item.a_path for item in self.repo.index.diff("HEAD")]
        return file_path_normalized in staged

    async def commit_fix(
        self,
        issue: SonarQubeIssue,
        files: list[str] | None,
        ai_tool_type: AIToolType,
        rule: SonarQubeRule | None = None,
    ) -> str | None:
        """Create a commit for a fixed issue.

        If files is None, automatically detects and commits all modified files.
        If files is an empty list, raises an error.

        Args:
            issue: The SonarQube issue that was fixed
            files: List of files to commit, or None to auto-detect all modified files
            ai_tool_type: The AI tool used for the fix
            rule: Detailed rule information (optional)

        Returns:
            Commit SHA, or None if there were no changes to commit

        Raises:
            GitOperationError: If commit fails or no files to commit
        """
        # Handle empty list case (explicit request with no files)
        if files is not None and not files:
            msg = "No files to commit"
            raise GitOperationError(msg)

        # Auto-detect modified files if not provided
        auto_detect = files is None
        if auto_detect:
            files = self.get_all_modified_files()

        # If no modified files in auto-detect mode, check if already fixed
        # This handles the case where one fix resolved multiple issues
        if auto_detect and not files and not self.repo.index.diff("HEAD"):
            # No modified files and nothing staged - issue was already fixed
            return None
        # If there are staged changes, proceed with those
        # (This shouldn't normally happen in auto-detect mode, but handle it gracefully)

        # At this point, files must be a list (either from auto-detect or passed in)
        # This should never happen due to the logic above, but satisfy type checker
        if files is None:
            msg = "Internal error: files should not be None at this point"
            raise GitOperationError(msg)

        # Determine correct file count for commit message
        if auto_detect and not files:
            staged_files_count = len(self.repo.index.diff("HEAD"))
            file_count_for_message = staged_files_count
        else:
            file_count_for_message = len(files)
        # Create commit message
        message = self._create_commit_message(issue, ai_tool_type, rule, file_count_for_message)

        # Stage and commit using helper method
        return await self._stage_and_commit(files, message)

    async def create_commit(
        self,
        message: str,
        files: list[str] | None = None,
        include_untracked: bool = False,
    ) -> str | None:
        """Create a commit with a custom message.

        If files is None, automatically detects and commits all modified files.

        Args:
            message: Commit message
            files: List of files to commit, or None to auto-detect all modified files
            include_untracked: If True, include untracked (new) files in auto-detection

        Returns:
            Commit SHA, or None if there were no changes to commit

        Raises:
            GitOperationError: If commit fails or no files to commit
        """
        # Auto-detect modified files if not provided
        if files is None:
            files = self.get_all_modified_files()
            # Add untracked files if requested (e.g., for deduplication when AI creates new files)
            if include_untracked:
                untracked = self.repo.untracked_files
                files.extend(untracked)

        if not files:
            msg = "No files to commit"
            raise GitOperationError(msg)

        # Stage and commit using helper method
        return await self._stage_and_commit(files, message)

    def _do_commit(self, message: str) -> git.Commit:
        """Delegate to repo.index.commit; extracted for testability.

        Args:
            message: Commit message

        Returns:
            Created commit object
        """
        return self.repo.index.commit(message)

    async def _run_pre_commit_hooks(self, files: list[str]) -> list[str]:
        """Run pre-commit hooks on staged files and return files modified by hooks.

        Args:
            files: List of files that were staged

        Returns:
            List of files modified by hooks that need to be re-staged
        """
        if self._pre_commit_command == "":
            return []

        repo_root = str(self.repo.working_dir)
        files_set = set(files)

        if self._pre_commit_command is not None:
            cmd = shlex.split(self._pre_commit_command) + files
        elif shutil.which("pre-commit"):
            cmd = ["pre-commit", "run", "--files", *files]
        else:
            cmd = ["git", "hook", "run", "--ignore-missing", "pre-commit"]

        result = await run(cmd, cwd=repo_root, timeout=self._pre_commit_timeout)
        if result.timed_out:
            msg = f"Pre-commit hook timed out after {self._pre_commit_timeout} seconds"
            raise GitOperationError(msg)
        if result.not_found:
            msg = f"Pre-commit hook failed to run: {result.stderr}"
            raise GitOperationError(msg)
        # Ignore return code; what matters is whether files were modified by hooks

        # Detect files modified by hooks (working tree differs from index)
        return [item.a_path for item in self.repo.index.diff(None) if item.a_path in files_set]

    def _stage_files(self, files: list[str]) -> list[str]:
        """Stage files, using index.remove for paths deleted from the working tree.

        Args:
            files: Paths to stage

        Returns:
            The subset of files that exist in the working tree
        """
        work_dir = Path(str(self.repo.working_dir))
        existing = [f for f in files if (work_dir / f).exists()]
        existing_set = set(existing)
        missing = [f for f in files if f not in existing_set]
        if existing:
            self.repo.index.add(existing)
        if missing:
            self.repo.index.remove(missing)
        return existing

    async def _stage_and_commit(self, files: list[str], message: str) -> str | None:
        """Stage files and create a commit if there are changes.

        Args:
            files: List of files to stage
            message: Commit message

        Returns:
            Commit SHA, or None if there were no changes to commit

        Raises:
            GitOperationError: If commit fails
        """
        try:
            # Stage files (if any)
            existing = self._stage_files(files)

            # Run pre-commit hooks and re-stage any auto-fixed files
            hook_modified = await self._run_pre_commit_hooks(existing)
            if hook_modified:
                self.repo.index.add(hook_modified)

            # Limit retry re-staging to files belonging to this commit
            staged_files = set(files) | set(hook_modified)

            # Check if there are any changes to commit after staging
            # This happens when one fix resolves multiple issues
            if not self.repo.index.diff("HEAD"):
                # No changes staged - return None to indicate no commit was created
                return None

            # Commit with one retry on failure (safety net for hooks that modify files)
            last_error: git.GitError = git.GitError("commit not attempted")
            for attempt in range(2):
                try:
                    commit = self._do_commit(message)
                except git.GitError as exc:
                    last_error = exc
                    if attempt == 1:
                        break
                    # Re-stage only files from this commit modified by hooks during commit attempt
                    retry_dirty = [item.a_path for item in self.repo.index.diff(None) if item.a_path in staged_files]
                    if not retry_dirty:
                        break
                    self.repo.index.add(retry_dirty)
                else:
                    return commit.hexsha
            raise last_error

        except (git.GitError, OSError) as e:
            msg = f"Failed to create commit: {e}"
            raise GitOperationError(msg) from e

    def _create_commit_message(
        self,
        issue: SonarQubeIssue,
        ai_tool_type: AIToolType,
        rule: SonarQubeRule | None = None,
        file_count: int = 1,
    ) -> str:
        """Create a formatted commit message for a fix.

        Args:
            issue: The SonarQube issue that was fixed
            ai_tool_type: The AI tool used for the fix
            rule: Detailed rule information (optional)
            file_count: Number of files modified in this fix (default: 1)

        Returns:
            Formatted commit message
        """
        # Extract rule name (e.g., "python:S1481" -> "S1481")
        rule_short = issue.rule.split(":")[-1] if ":" in issue.rule else issue.rule

        # Create subject line (use rule name if available)
        if rule:
            subject = f"fix: [{issue.rule}] {issue.message[:50]}"
        else:
            subject = f"fix: [SQ-{rule_short}] {issue.message[:50]}"

        if len(issue.message) > 50:
            subject = subject.rstrip() + "..."

        # Create body
        body_parts = [
            f"SonarQube Issue: {issue.key}",
        ]

        if rule:
            body_parts.append(f"Rule: {issue.rule} - {rule.name}")
        else:
            body_parts.append(f"Rule: {issue.rule}")

        body_parts.extend([
            f"Severity: {issue.severity}",
            f"Location: {issue.component}:{issue.line}",
        ])

        # Add file count if multiple files were modified
        if file_count > 1:
            body_parts.append(f"Files modified: {file_count}")

        body_parts.append("")

        # Add public documentation link and collapsed root-cause explanation
        if rule:
            body_parts.extend([
                f"Rationale: {rule.public_doc_url}",
                "",
            ])
            details = rule.as_details_block()
            if details:
                body_parts.extend([details, ""])

        body_parts.extend([
            f"Fixed by: vibe_heal using {ai_tool_type.display_name}",
            "",
            "[vibe_heal](https://github.com/alexeieleusis/dotharness)",
        ])

        body = "\n".join(body_parts)

        # Combine subject and body
        return f"{subject}\n\n{body}"

    def require_clean_working_directory(self) -> None:
        """Ensure working directory has no modified or staged files.

        Untracked files are allowed, but modified or staged files must be committed first.

        Raises:
            DirtyWorkingDirectoryError: If there are modified or staged files
        """
        modified_or_staged = self.get_modified_or_staged_files()
        if modified_or_staged:
            msg = (
                "Working directory has uncommitted changes. Please commit or stash modified files first.\n"
                "Modified or staged files:\n"
                + "\n".join(f"  - {f}" for f in modified_or_staged[:10])
                + ("\n  ..." if len(modified_or_staged) > 10 else "")
                + "\n\nNote: Untracked files are OK and won't block processing."
            )
            raise DirtyWorkingDirectoryError(msg)
