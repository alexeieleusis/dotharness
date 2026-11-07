"""Git branch analysis for identifying modified files."""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from git import GitCommandError, Repo
from git.exc import InvalidGitRepositoryError

from dotharness.vibe_heal.git.exceptions import GitOperationError
from dotharness.vibe_heal.output import warn

# Upper bound for any single git subprocess so a stalled network never hangs the CLI.
GIT_COMMAND_TIMEOUT_SECONDS = 120


class BranchAnalyzerError(Exception):
    """Base exception for BranchAnalyzer errors."""

    pass


class BranchNotFoundError(BranchAnalyzerError):
    """Raised when a specified branch does not exist."""

    pass


class InvalidRepositoryError(BranchAnalyzerError):
    """Raised when the repository is invalid or not found."""

    pass


class BranchAnalyzer:
    """Analyzes differences between git branches to identify modified files.

    This class provides functionality to compare the current branch with a base
    branch and identify which files have been modified, added, or changed.
    """

    def __init__(self, repo_path: Path) -> None:
        """Initialize the BranchAnalyzer.

        Args:
            repo_path: Path to the git repository root

        Raises:
            InvalidRepositoryError: If repo_path is not a valid git repository
        """
        try:
            self.repo = Repo(repo_path, search_parent_directories=True)
        except InvalidGitRepositoryError as e:
            msg = f"Not a valid git repository: {repo_path}"
            raise InvalidRepositoryError(msg) from e

        self.repo_path = Path(self.repo.working_dir)

    def get_modified_files(self, base_branch: str = "origin/main") -> list[Path]:
        """Get list of files modified in current branch vs. base branch.

        Uses git diff to find files that differ between the base branch and HEAD.
        Only returns files that currently exist in the working tree (excludes deletions).

        Args:
            base_branch: Name of the base branch to compare against. Defaults to 'origin/main'.

        Returns:
            List of Path objects for modified files, relative to the current working directory.
            When running from a monorepo subdirectory, only files under that directory are returned.
            Returns empty list if no files are modified.

        Raises:
            BranchNotFoundError: If base_branch does not exist in the repository
            BranchAnalyzerError: If git command fails for other reasons
        """
        # Validate base branch exists
        if not self.validate_branch_exists(base_branch):
            msg = f"Branch '{base_branch}' does not exist in repository"
            raise BranchNotFoundError(msg)

        try:
            # Use three-dot diff syntax to compare merge base
            # This shows changes on current branch since it diverged from base_branch
            # -z emits NUL-separated, unquoted paths so non-ASCII/special names survive
            diff_output = self.repo.git.diff("--name-only", "-z", f"{base_branch}...HEAD")

            if not diff_output.strip("\0"):
                return []

            # Parse file paths from diff output
            file_paths = [path for path in diff_output.split("\0") if path]

            # Determine if we're running from a subdirectory of the repo (monorepo support)
            cwd = Path.cwd().resolve()
            repo_root = self.repo_path.resolve()

            try:
                cwd_relative_to_repo = cwd.relative_to(repo_root)
            except ValueError:
                # CWD is not under repo root (shouldn't happen) — use repo root behavior
                cwd_relative_to_repo = Path(".")

            running_from_subdir = cwd_relative_to_repo != Path(".")

            # Filter to only existing files (exclude deletions)
            existing_files: list[Path] = []
            for file_path in file_paths:
                repo_relative_path = Path(file_path)
                full_path = self.repo_path / file_path

                if not (full_path.exists() and full_path.is_file()):
                    continue

                if running_from_subdir:
                    # Only include files under CWD; return path relative to CWD
                    try:
                        local_path = repo_relative_path.relative_to(cwd_relative_to_repo)
                    except ValueError:
                        # File belongs to a different project — skip
                        continue
                    existing_files.append(local_path)
                else:
                    # Running from repo root — keep original behavior
                    existing_files.append(repo_relative_path)

        except GitCommandError as e:
            msg = f"Git diff command failed: {e}"
            raise BranchAnalyzerError(msg) from e
        else:
            return existing_files

    def get_current_branch(self) -> str:
        """Get name of the current active branch.

        Returns:
            Name of current branch (e.g., 'feature/new-api', 'main')

        Raises:
            BranchAnalyzerError: If unable to determine current branch (e.g., detached HEAD)
        """

        def _raise_if_detached() -> None:
            if self.repo.head.is_detached:
                msg = "Repository is in detached HEAD state"
                raise BranchAnalyzerError(msg)

        try:
            _raise_if_detached()
            branch_name = self.repo.active_branch.name

        except Exception as e:
            msg = f"Failed to get current branch: {e}"
            raise BranchAnalyzerError(msg) from e
        else:
            return branch_name

    def get_head_sha(self) -> str:
        """Get the full commit SHA of the current HEAD.

        Returns:
            Full HEAD commit SHA (e.g. 'abcdef1234...')

        Raises:
            BranchAnalyzerError: If HEAD has no commit (e.g. empty repository)
        """
        try:
            return self.repo.head.commit.hexsha
        except Exception as e:
            msg = f"Failed to get HEAD commit SHA: {e}"
            raise BranchAnalyzerError(msg) from e

    def validate_branch_exists(self, branch: str) -> bool:
        """Check if a branch exists in the repository.

        Checks both local and remote branches. Supports both simple names (e.g., 'main')
        and remote refs (e.g., 'origin/main').

        Args:
            branch: Name of the branch to check

        Returns:
            True if branch exists (locally or remotely), False otherwise
        """
        try:
            # Check local branches first: names like 'release/1.0' contain a slash
            # but are local, not '<remote>/<branch>'.
            local_branches = [ref.name for ref in self.repo.branches]
            if branch in local_branches:
                return True

            # Check if it's a remote ref (e.g., origin/main)
            if "/" in branch:
                remote_name, branch_name = branch.split("/", 1)
                try:
                    remote = self.repo.remote(remote_name)
                    remote_branches = [ref.name for ref in remote.refs]
                except Exception:
                    # Not a known remote; fall through to default-remote check
                    remote_branches = []
                if f"{remote_name}/{branch_name}" in remote_branches:
                    return True

            # Check default remote branches (origin/branch)
            try:
                remote_branches = [ref.name.split("/", 1)[1] for ref in self.repo.remote().refs]
                if branch in remote_branches:
                    return True
            except GitCommandError:
                # Cannot access default remote, assume branch doesn't exist
                pass

        except Exception:
            # If we can't list branches, assume it doesn't exist
            return False
        else:
            return False

    def get_user_email(self) -> str:
        """Get configured git user email for project naming.

        Returns:
            Git user email from repository or global config

        Raises:
            BranchAnalyzerError: If user email is not configured
        """

        def _validate_email_is_string(email: str | int | float | None) -> str:
            """Validate that email is a non-empty string."""
            if not email or not isinstance(email, str):
                msg = "Git user email not configured. Run: git config user.email 'your@email.com'"
                raise BranchAnalyzerError(msg)
            return email

        try:
            # Try repository config first, then global config
            email = self.repo.config_reader().get_value("user", "email", default=None)
            return _validate_email_is_string(email)

        except BranchAnalyzerError:
            # Re-raise our own exceptions
            raise
        except Exception as e:
            msg = f"Failed to get user email: {e}"
            raise BranchAnalyzerError(msg) from e

    def _run_git(
        self, args: list[str], *, capture_stdout: bool = False, capture_stderr: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a git command with a timeout and without interactive prompts.

        Raises:
            GitOperationError: If the command times out.
            OSError: If the git binary cannot be executed.
        """
        try:
            return subprocess.run(  # noqa: S603
                ["git", *args],  # noqa: S607
                cwd=self.repo_path,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=GIT_COMMAND_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            msg = f"'git {' '.join(args)}' timed out after {GIT_COMMAND_TIMEOUT_SECONDS}s"
            raise GitOperationError(msg) from e

    def split_remote_ref(self, ref: str) -> tuple[str, str] | None:
        """Split a ref into ``(remote, branch)`` if its prefix is a real remote.

        ``origin/main`` -> ``("origin", "main")``. A bare name (``main``) or a local
        branch containing a slash (``release/1.0``) has no remote prefix and yields
        ``None``. The longest matching remote name wins.

        Raises:
            GitOperationError: If ``git remote`` cannot be run or fails.
        """
        try:
            result = self._run_git(["remote"], capture_stdout=True)
        except OSError as e:
            msg = f"Failed to run 'git remote': {e}"
            raise GitOperationError(msg) from e
        if result.returncode != 0:
            msg = f"'git remote' exited with code {result.returncode}"
            raise GitOperationError(msg)
        remotes = result.stdout.decode(errors="replace").split() if result.stdout else []
        for remote in sorted(remotes, key=len, reverse=True):
            prefix = f"{remote}/"
            if ref.startswith(prefix) and len(ref) > len(prefix):
                return remote, ref[len(prefix) :]
        return None

    def fetch_remote_ref(self, ref: str) -> bool:
        """Fetch a remote-tracking ref (``<remote>/<branch>``).

        Returns:
            True if fetched, False if ``ref`` is not a remote-tracking ref (nothing to fetch).

        Raises:
            GitOperationError: If the git binary is unavailable or the fetch fails.
        """
        split = self.split_remote_ref(ref)
        if split is None:
            return False
        remote, branch = split
        try:
            result = self._run_git(["fetch", "--", remote, branch], capture_stderr=True)
        except OSError as e:
            msg = f"Failed to run 'git fetch {remote} {branch}': {e}"
            raise GitOperationError(msg) from e
        if result.returncode != 0:
            stderr_text = result.stderr.decode(errors="replace").strip() if result.stderr else ""
            detail = stderr_text or f"'git fetch {remote} {branch}' exited with code {result.returncode}"
            msg = f"Refusing to run: unable to fetch {ref}. {detail}"
            raise GitOperationError(msg)
        return True

    def is_ancestor_of_head(self, ref: str) -> bool:
        """Check whether ``ref`` is an ancestor of HEAD (a merged-in ref counts).

        Raises:
            GitOperationError: If the git binary cannot be run.
        """
        try:
            result = self._run_git(["merge-base", "--is-ancestor", ref, "HEAD"])
        except OSError as e:
            msg = f"Failed to check ancestry with {ref}: {e}"
            raise GitOperationError(msg) from e
        return result.returncode == 0

    @contextmanager
    def temporary_worktree(self, ref: str) -> Generator[Path]:
        """Check ``ref`` out into a temporary detached worktree, removed on exit.

        The main working tree is untouched. Removal failures are warnings, never errors.

        Raises:
            GitOperationError: If the worktree cannot be created.
        """
        worktree_dir = Path(tempfile.mkdtemp(prefix="vibe-heal-baseline-"))
        created = False
        try:
            try:
                result = self._run_git(["worktree", "add", "--detach", str(worktree_dir), ref], capture_stderr=True)
            except OSError as e:
                msg = f"Failed to create worktree for {ref}: {e}"
                raise GitOperationError(msg) from e
            if result.returncode != 0:
                stderr_text = result.stderr.decode(errors="replace").strip() if result.stderr else ""
                msg = f"Failed to create worktree for {ref}: {stderr_text or f'exit code {result.returncode}'}"
                raise GitOperationError(msg)
            created = True
            yield worktree_dir
        finally:
            if created:
                try:
                    removed = self._run_git(["worktree", "remove", "--force", str(worktree_dir)])
                    if removed.returncode != 0:
                        warn(f"Warning: Failed to remove worktree {worktree_dir}")
                except (OSError, GitOperationError) as e:
                    warn(f"Warning: Failed to remove worktree {worktree_dir}: {e}")
            # Also drops the mkdtemp directory when the worktree was never created.
            shutil.rmtree(worktree_dir, ignore_errors=True)
