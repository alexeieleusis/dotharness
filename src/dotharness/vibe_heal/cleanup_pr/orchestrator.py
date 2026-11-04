"""Branch PR cleanup orchestration (cleanup-pr command).

Skeleton for the ``cleanup-pr`` workflow. This module owns the strict
preconditions (FR-2 step 1 + 11.A-12), file selection (step 2), the temporary
SonarQube project lifecycle (step 4 + step 7), and the top-level failure policy
(11.A-8 / 11.A-14, same as ``cleanup``). The analysis + per-file fix loop
(FR-2 steps 5-6) lives in ``_run_iteration_loop``: analyze, recompute the diff, fix
in-scope duplications then in-scope issues per file, repeat.
"""

import asyncio
from pathlib import Path

from pydantic import BaseModel

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.cleanup_pr.main_duplications import (
    MainDuplicationTask,
    build_main_duplication_commit_message,
    build_main_duplication_prompt,
    build_main_duplication_task,
)
from dotharness.vibe_heal.cleanup_pr.scope import select_in_scope_duplications, select_in_scope_issues
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.client import DuplicationClient
from dotharness.vibe_heal.deduplication.models import DuplicationGroup
from dotharness.vibe_heal.deduplication.orchestrator import DeduplicationOrchestrator
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer, BranchNotFoundError
from dotharness.vibe_heal.git.diff_parser import DiffLines, DiffParser
from dotharness.vibe_heal.git.exceptions import GitOperationError, NotAGitRepositoryError
from dotharness.vibe_heal.git.file_selection import select_modified_files
from dotharness.vibe_heal.git.manager import GitManager
from dotharness.vibe_heal.git.paths import to_repo_relative
from dotharness.vibe_heal.orchestrator import VibeHealOrchestrator
from dotharness.vibe_heal.output import bold, dim, error, success, warn
from dotharness.vibe_heal.processor.issue_processor import IssueProcessor
from dotharness.vibe_heal.review.duplication_scope import get_resolved_duplications
from dotharness.vibe_heal.review.models import FileDiagnostics
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult, AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import ComponentNotFoundError
from dotharness.vibe_heal.sonarqube.project_manager import ProjectManager, TempProjectMetadata


class FileCleanupPrResult(BaseModel):
    """Result of cleaning up a single file (PR-scoped)."""

    file_path: Path
    issues_fixed: int = 0
    issues_out_of_scope: int = 0
    duplications_fixed: int = 0
    duplications_out_of_scope: int = 0
    main_duplications_fixed: int = 0
    main_duplications_skipped: int = 0
    success: bool
    error_message: str | None = None


class CleanupPrResult(BaseModel):
    """Result of the branch PR cleanup operation."""

    success: bool
    files_processed: list[FileCleanupPrResult]
    temp_project: TempProjectMetadata | None = None
    analysis_result: AnalysisResult | None = None
    total_issues_fixed: int = 0
    total_duplications_fixed: int = 0
    total_main_duplications_fixed: int = 0
    external_files_touched: list[Path] = []
    error_message: str | None = None


class CleanupPrAnalysisError(Exception):
    """Raised by the iteration loop when a SonarQube analysis round fails.

    Carries the partial state so ``cleanup_pr`` can return a failed result that
    still reports what was done before the failure.
    """

    def __init__(
        self,
        iteration: int,
        analysis_result: AnalysisResult,
        files_processed: list[FileCleanupPrResult],
        external_files_touched: list[Path],
    ) -> None:
        super().__init__(f"Analysis failed at iteration {iteration}: {analysis_result.error_message}")
        self.analysis_result = analysis_result
        self.files_processed = files_processed
        self.external_files_touched = external_files_touched


def _target_range(group: DuplicationGroup, target_ref: str) -> tuple[int, int] | None:
    """Identify a duplication group by its target block's line range."""
    block = group.get_target_block(target_ref)
    return None if block is None else (block.from_line, block.to_line)


class CleanupPrOrchestrator:
    """Orchestrates the branch PR cleanup workflow.

    Coordinates:
    - Strict preconditions (repo, base branch, AI tool, clean tree, fetch, up-to-date)
    - File selection (modified vs. base)
    - Temporary project creation
    - SonarQube analysis + per-file duplications-then-issues fix loop
    - Project cleanup
    """

    def __init__(
        self,
        config: VibeHealConfig,
        client: SonarQubeClient,
        ai_tool: AITool,
        diff_parser: DiffParser | None = None,
    ) -> None:
        """Initialize the cleanup-pr orchestrator.

        Args:
            config: Application configuration
            client: SonarQube API client
            ai_tool: AI tool for fixing issues
            diff_parser: Optional DiffParser instance (for testing)
        """
        self.config = config
        self.client = client
        self.ai_tool = ai_tool
        self.project_manager = ProjectManager(client)
        self.analysis_runner = AnalysisRunner(config, client)
        self.branch_analyzer = BranchAnalyzer(Path.cwd())
        self.git_manager = GitManager(Path.cwd(), pre_commit_command=config.pre_commit_command)
        self.diff_parser = diff_parser if diff_parser is not None else DiffParser(Path.cwd())
        # Repo-relative files touched by fix commits since the current analysis; their line numbers are stale.
        self._stale_files: set[str] = set()

    async def cleanup_pr(
        self,
        base_branch: str = "origin/main",
        max_iterations: int = 10,
        file_patterns: list[str] | None = None,
        min_severity: str | None = None,
        include_main_duplications: bool = False,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> CleanupPrResult:
        """Clean up SonarQube issues and duplications scoped to branch changes.

        The strict preconditions (FR-2 step 1 + 11.A-12) run up front, before any
        SonarQube work: the tree must be a clean git repository sitting on top of
        a freshly fetched base branch, with an available AI tool unless this is a
        dry run. With ``include_main_duplications`` a baseline scan of the base ref
        (FR-2 step 3) refreshes the real project's analysis before the temp project
        is created; it always runs when the flag is set, including in dry-run.

        Args:
            base_branch: Base branch to fetch and compare against (default: origin/main)
            max_iterations: Maximum analysis iterations for the whole branch (default: 10)
            file_patterns: Optional list of glob patterns to filter files
            min_severity: Minimum issue severity to fix (passed through to the loop)
            include_main_duplications: Run the baseline scan of the base ref against the real project (FR-2 step 3)
            dry_run: Preview without committing (skips the AI-tool availability check)
            verbose: Enable verbose output

        Returns:
            CleanupPrResult with success status and details
        """
        # FR-2 step 1 + 11.A-12: strict preconditions, all before any SonarQube work.
        self._validate_preconditions(base_branch, dry_run)
        self._fetch_base_branch(base_branch)
        self._check_up_to_date_with_base(base_branch)

        temp_project: TempProjectMetadata | None = None
        original_project_key: str | None = None
        files_processed: list[FileCleanupPrResult] = []
        # Owned here so an unexpected mid-run exception still reports earlier progress.
        loop_results: dict[Path, FileCleanupPrResult] = {}
        loop_external_files: list[Path] = []

        try:
            # Step 2: select and filter the modified files.
            modified_files = select_modified_files(self.branch_analyzer, base_branch, file_patterns)

            if not modified_files:
                return CleanupPrResult(success=True, files_processed=[])

            # Step 3: baseline scan of the base ref into the real project (flag only).
            # Runs before the temp project exists, so a failure needs no deletion.
            if include_main_duplications:
                baseline_result = await self._run_baseline_scan(base_branch)
                if not baseline_result.success:
                    msg = f"Baseline scan failed: {baseline_result.error_message}"
                    error(msg)
                    return CleanupPrResult(
                        success=False,
                        files_processed=[],
                        analysis_result=baseline_result,
                        error_message=msg,
                    )

            # Step 4: create the temporary SonarQube project.
            temp_project = await self._create_temp_project()

            # Project-key override: route the whole run to the temp project. Both keys
            # are restored in the finally block on every path (same pattern as cleanup).
            original_project_key = self.config.sonarqube_project_key
            self.config.sonarqube_project_key = temp_project.project_key
            self.client.config.sonarqube_project_key = temp_project.project_key

            # Steps 5-6: iteration/fix loop (analysis + per-file duplications-then-issues).
            (
                files_processed,
                analysis_result,
                external_files_touched,
            ) = await self._run_iteration_loop(
                modified_files=modified_files,
                temp_project=temp_project,
                base_branch=base_branch,
                max_iterations=max_iterations,
                min_severity=min_severity,
                dry_run=dry_run,
                verbose=verbose,
                include_main_duplications=include_main_duplications,
                original_project_key=original_project_key,
                results=loop_results,
                external_files=loop_external_files,
            )

            return CleanupPrResult(
                success=True,
                files_processed=files_processed,
                temp_project=temp_project,
                analysis_result=analysis_result,
                total_issues_fixed=sum(f.issues_fixed for f in files_processed),
                total_duplications_fixed=sum(f.duplications_fixed for f in files_processed),
                total_main_duplications_fixed=sum(f.main_duplications_fixed for f in files_processed),
                external_files_touched=external_files_touched,
            )

        except CleanupPrAnalysisError as e:
            error(str(e))
            return CleanupPrResult(
                success=False,
                files_processed=e.files_processed,
                temp_project=temp_project,
                analysis_result=e.analysis_result,
                total_issues_fixed=sum(f.issues_fixed for f in e.files_processed),
                total_duplications_fixed=sum(f.duplications_fixed for f in e.files_processed),
                total_main_duplications_fixed=sum(f.main_duplications_fixed for f in e.files_processed),
                external_files_touched=e.external_files_touched,
                error_message=str(e),
            )

        except Exception as e:
            # Earlier commits are kept, so report the progress made before the failure.
            partial = list(loop_results.values()) or files_processed
            return CleanupPrResult(
                success=False,
                files_processed=partial,
                temp_project=temp_project,
                total_issues_fixed=sum(f.issues_fixed for f in partial),
                total_duplications_fixed=sum(f.duplications_fixed for f in partial),
                total_main_duplications_fixed=sum(f.main_duplications_fixed for f in partial),
                external_files_touched=loop_external_files,
                error_message=f"Cleanup failed: {e}",
            )

        finally:
            # Restore the original project key on every path.
            if original_project_key is not None:
                self.config.sonarqube_project_key = original_project_key
                self.client.config.sonarqube_project_key = original_project_key
            # Step 7: always clean up the temp project.
            await self._cleanup_temp_project(temp_project)

    # ------------------------------------------------------------------
    # Preconditions (FR-2 step 1 + 11.A-12)
    # ------------------------------------------------------------------

    def _validate_preconditions(self, base_branch: str, dry_run: bool) -> None:
        """Validate the strict preconditions before any SonarQube work.

        Args:
            base_branch: Base branch to compare against.
            dry_run: Whether this is a dry run (skips the AI-tool availability check).

        Raises:
            NotAGitRepositoryError: If not in a git repository.
            BranchNotFoundError: If the base branch does not exist.
            RuntimeError: If the AI tool is unavailable and this is not a dry run.
            DirtyWorkingDirectoryError: If the working tree has modified or staged files.
        """
        if not self.git_manager.is_repository():
            msg = "Not a Git repository"
            raise NotAGitRepositoryError(msg)

        if not self.branch_analyzer.validate_branch_exists(base_branch):
            msg = f"Branch '{base_branch}' does not exist in repository"
            raise BranchNotFoundError(msg)

        if not dry_run and not self.ai_tool.is_available():
            msg = f"{self.ai_tool.tool_type.display_name} is not available"
            raise RuntimeError(msg)

        # Strict clean working tree (untracked files are fine), checked once up front.
        self.git_manager.require_clean_working_directory()

    def _fetch_base_branch(self, base_branch: str) -> None:
        """Fetch the base ref's remote ref; refuse to run on any fetch failure.

        Runs even in dry-run. On any failure (offline, auth, missing remote) the
        run is refused with a domain error before any SonarQube work — no fallback
        to the local ref. A base ref that is not ``<remote>/<branch>`` (a local
        branch) is not fetched; a warning says so.

        Args:
            base_branch: Base branch to fetch (e.g. 'origin/main').

        Raises:
            GitOperationError: If the git binary is unavailable or the fetch fails.
        """
        dim(f"Fetching {base_branch} ...")
        try:
            fetched = self.branch_analyzer.fetch_remote_ref(base_branch)
        except GitOperationError as e:
            error(str(e))
            raise
        if not fetched:
            warn(f"'{base_branch}' is not a remote-tracking ref; skipping fetch and using the local ref as-is")

    def _check_up_to_date_with_base(self, base_branch: str) -> None:
        """Ensure HEAD sits on top of the base branch (base is an ancestor of HEAD).

        A merged-in base counts; linear history is not required. Refuses to run
        otherwise, before any SonarQube work.

        Args:
            base_branch: Base branch to check against.

        Raises:
            GitOperationError: If HEAD is not up to date with the base branch.
        """
        dim(f"Checking that HEAD is up to date with {base_branch} ...")
        try:
            up_to_date = self.branch_analyzer.is_ancestor_of_head(base_branch)
        except GitOperationError as e:
            error(str(e))
            raise
        if not up_to_date:
            msg = f"Branch is not up to date with {base_branch}; rebase or merge {base_branch} first"
            error(msg)
            raise GitOperationError(msg)

    # ------------------------------------------------------------------
    # Baseline scan (FR-2 step 3, only with --include-main-duplications)
    # ------------------------------------------------------------------

    async def _run_baseline_scan(self, base_branch: str) -> AnalysisResult:
        """Refresh the real project's analysis so it reflects ``base_branch``.

        Checks the base ref out into a temporary ``git worktree`` (git state of the
        working tree is untouched), runs a full analysis against the real project
        key from there, and removes the worktree in a ``finally`` on every path.
        Always runs when called: no up-to-date check (11.A-13). This overwrites the
        real project's analysis on the server, including in dry-run.

        Args:
            base_branch: Base ref to scan (e.g. 'origin/main').

        Returns:
            The AnalysisResult of the baseline scan.

        Raises:
            GitOperationError: If the worktree cannot be created.
        """
        project_key = self.config.sonarqube_project_key
        with self.branch_analyzer.temporary_worktree(base_branch) as worktree_dir:
            dim(f"Running baseline SonarQube scan of {base_branch} against {project_key}...")
            return await self.analysis_runner.run_analysis(
                project_key=project_key,
                project_name=project_key,
                project_dir=worktree_dir,
            )

    # ------------------------------------------------------------------
    # Temp project lifecycle (FR-2 step 4 + step 7)
    # ------------------------------------------------------------------

    async def _create_temp_project(self) -> TempProjectMetadata:
        """Create a temporary SonarQube project for analysis.

        Returns:
            Metadata for the created temp project.
        """
        current_branch = self.branch_analyzer.get_current_branch()
        user_email = self.branch_analyzer.get_user_email()

        return await self.project_manager.create_temp_project_with_settings(
            base_key=self.config.sonarqube_project_key,
            branch_name=current_branch,
            user_email=user_email,
            command_name="cleanup-pr",
        )

    async def _cleanup_temp_project(self, temp_project: TempProjectMetadata | None) -> None:
        """Delete the temp project; a deletion failure is a warning, not an error.

        Args:
            temp_project: Temp project metadata (None if not created).
        """
        if temp_project:
            try:
                dim(f"Deleting temporary project: {temp_project.project_key}")
                await self.project_manager.delete_project(temp_project.project_key)
                success("✓ Temporary project deleted")
            except Exception as e:
                warn(f"Warning: Failed to delete temporary project: {e}")

    # ------------------------------------------------------------------
    # Iteration/fix loop (FR-2 steps 5-6)
    # ------------------------------------------------------------------

    async def _run_iteration_loop(
        self,
        modified_files: list[Path],
        temp_project: TempProjectMetadata,
        base_branch: str,
        max_iterations: int,
        min_severity: str | None,
        dry_run: bool,
        verbose: bool,
        include_main_duplications: bool = False,
        original_project_key: str | None = None,
        results: dict[Path, FileCleanupPrResult] | None = None,
        external_files: list[Path] | None = None,
    ) -> tuple[list[FileCleanupPrResult], AnalysisResult | None, list[Path]]:
        """Run the analysis + per-file duplications-then-issues fix loop (FR-2 steps 5-6).

        Each iteration runs a full-repo analysis, recomputes the diff (earlier fix
        commits shift line numbers), then per file fixes in-scope duplications first
        and in-scope issues second. Stops early when nothing in scope remains. Under
        ``dry_run`` nothing is committed, so a single analysis/scoping round is run.

        Args:
            modified_files: Selected files to process.
            temp_project: Active temp project (keys are already overridden by the caller).
            base_branch: Base branch the diff is computed against.
            max_iterations: Maximum analysis iterations for the whole branch.
            min_severity: Minimum issue severity to fix.
            dry_run: Preview without committing.
            verbose: Enable verbose output.
            include_main_duplications: First fix main duplications (FR-6, FR-2 step 5) before the loop.
            original_project_key: The real project's key (config points at the temp project); required
                with ``include_main_duplications``.
            results: Optional caller-owned dict filled with per-file results, so the caller can
                still report progress if the loop raises unexpectedly.
            external_files: Optional caller-owned list of external files touched (same purpose).

        Returns:
            Tuple of (per-file results, analysis result, external files touched).

        Raises:
            CleanupPrAnalysisError: If an analysis round fails.
        """
        if results is None:
            results = {}
        if external_files is None:
            external_files = []
        results.update({f: FileCleanupPrResult(file_path=f, success=True) for f in modified_files})
        analysis_result: AnalysisResult | None = None

        # FR-2 step 5: main duplications first (11.A-3), before any other fix commit. When
        # nothing was committed the analysis is still fresh and the first iteration reuses it.
        reusable_analysis: AnalysisResult | None = None
        if include_main_duplications:
            if original_project_key is None:
                msg = "original_project_key is required with include_main_duplications"
                raise ValueError(msg)
            reusable_analysis = await self._run_main_duplication_phase(
                modified_files=modified_files,
                temp_project=temp_project,
                base_branch=base_branch,
                original_project_key=original_project_key,
                results=results,
                external_files=external_files,
                dry_run=dry_run,
                verbose=verbose,
            )

        for iteration in range(max_iterations):
            bold(f"\nIteration {iteration + 1}/{max_iterations}")

            # Step 6.1: full-repo analysis into the temp project (reused from the
            # main-duplication phase when nothing was committed there).
            analysis_result = await self._run_analysis_round(
                iteration=iteration + 1,
                reusable_analysis=reusable_analysis,
                temp_project=temp_project,
                results=results,
                external_files=external_files,
            )
            reusable_analysis = None

            # Step 6.2: recompute the diff every iteration (fix commits shift lines).
            diff_lines = self.diff_parser.get_diff_lines(base_branch)
            diff_files = self._diff_files(diff_lines, modified_files)

            # Step 6.3: duplications first, then issues, per file.
            self._stale_files.clear()
            in_scope_remaining = 0
            for file_path in modified_files:
                in_scope_remaining += await self._process_file(
                    file_path=file_path,
                    result=results[file_path],
                    diff_lines=diff_lines,
                    diff_files=diff_files,
                    external_files=external_files,
                    temp_project=temp_project,
                    min_severity=min_severity,
                    dry_run=dry_run,
                    verbose=verbose,
                )

            # Step 6.4: stop early when nothing in scope remains.
            if in_scope_remaining == 0:
                success("✓ No in-scope issues or duplications remaining!")
                break

            if dry_run:
                # Nothing is committed in a dry run, so another round would see the same findings.
                break

            # Step 6.5: wait before the next analysis, as cleanup does.
            if iteration < max_iterations - 1:
                dim("Waiting for SonarQube to process changes...")
                await asyncio.sleep(5)

        return list(results.values()), analysis_result, external_files

    async def _run_analysis_round(
        self,
        iteration: int,
        reusable_analysis: AnalysisResult | None,
        temp_project: TempProjectMetadata,
        results: dict[Path, FileCleanupPrResult],
        external_files: list[Path],
    ) -> AnalysisResult:
        """Run (or reuse) the full-repo analysis for one iteration (FR-2 step 6.1).

        A reusable analysis from the main-duplication phase is consumed as-is;
        otherwise a fresh analysis runs into the temp project.

        Args:
            iteration: 1-based iteration number (used in the failure message).
            reusable_analysis: Fresh analysis from the main-duplication phase, or
                None when no reuse applies.
            temp_project: Active temp project the analysis runs into.
            results: Per-file results so far (carried on a failure).
            external_files: External files touched so far (carried on a failure).

        Returns:
            The analysis result for this iteration.

        Raises:
            CleanupPrAnalysisError: If the analysis round fails.
        """
        if reusable_analysis is not None:
            dim("Reusing the analysis from the main-duplication phase (no commits were made).")
            return reusable_analysis
        dim("Running SonarQube analysis on full repository...")
        analysis_result = await self.analysis_runner.run_analysis(
            project_key=temp_project.project_key,
            project_name=temp_project.project_name,
            project_dir=Path.cwd(),
        )
        if not analysis_result.success:
            raise CleanupPrAnalysisError(iteration, analysis_result, list(results.values()), external_files)
        dim(f"Analysis completed. Dashboard: {analysis_result.dashboard_url}")
        return analysis_result

    def _diff_files(self, diff_lines: DiffLines, modified_files: list[Path]) -> set[str]:
        """Repo-relative paths of every file in the branch diff (plus the selected files)."""
        diff_files = set(diff_lines.new_lines) | set(diff_lines.old_lines) | set(diff_lines.strict_new_lines)
        diff_files |= {self._to_repo_relative(f) for f in modified_files}
        return diff_files

    # ------------------------------------------------------------------
    # Main duplications (FR-2 step 5 + FR-6, only with --include-main-duplications)
    # ------------------------------------------------------------------

    async def _run_main_duplication_phase(
        self,
        modified_files: list[Path],
        temp_project: TempProjectMetadata,
        base_branch: str,
        original_project_key: str,
        results: dict[Path, FileCleanupPrResult],
        external_files: list[Path],
        dry_run: bool,
        verbose: bool,
    ) -> AnalysisResult | None:
        """Analyze the branch, then detect and fix main duplications (once, from the original diff).

        The qualifying set is computed before the first fix commit, so ``old_lines`` still
        describes the original diff. Each task's branch-side hunk/range is nevertheless
        re-read from HEAD right before its AI call (``_fix_main_duplication``), since
        earlier fix commits in the same file shift line numbers. Failure policy is the same
        as elsewhere: a failed AI attempt increments ``failed`` and processing continues;
        nothing is reverted.

        Returns:
            The analysis result when it is still fresh (no commits were made), else None
            so that the iteration loop starts with a new analysis.

        Raises:
            CleanupPrAnalysisError: If the analysis fails.
        """
        bold("\nMain duplications")
        dim("Running SonarQube analysis on full repository...")
        analysis_result = await self.analysis_runner.run_analysis(
            project_key=temp_project.project_key,
            project_name=temp_project.project_name,
            project_dir=Path.cwd(),
        )
        if not analysis_result.success:
            raise CleanupPrAnalysisError(1, analysis_result, list(results.values()), external_files)
        dim(f"Analysis completed. Dashboard: {analysis_result.dashboard_url}")

        diff_lines = self.diff_parser.get_diff_lines(base_branch)
        diff_files = self._diff_files(diff_lines, modified_files)
        merge_base = str(self.branch_analyzer.repo.git.merge_base(base_branch, "HEAD")).strip()

        tasks: list[MainDuplicationTask] = []
        for file_path in modified_files:
            tasks.extend(
                await self._detect_main_duplications(
                    file_path, diff_lines, temp_project, original_project_key, merge_base, results[file_path], verbose
                )
            )

        if not tasks:
            dim("No main duplications to fix.")
            return analysis_result

        commits = 0
        for task in tasks:
            commits += await self._fix_main_duplication(
                task, results[task.file_path], diff_files, external_files, dry_run, merge_base
            )
        return None if commits else analysis_result

    async def _detect_main_duplications(
        self,
        file_path: Path,
        diff_lines: DiffLines,
        temp_project: TempProjectMetadata,
        original_project_key: str,
        merge_base: str,
        result: FileCleanupPrResult,
        verbose: bool,
    ) -> list[MainDuplicationTask]:
        """Detect a file's qualifying main duplications (FR-6 Detection).

        Queries the REAL project (repo-relative path), keeps blocks that intersect the
        old-side changed lines and overlap no active duplication of the temp project.
        Blocks whose task cannot be built (no main-side text, no diff, or no branch hunk
        related to the block) are counted in ``result.main_duplications_skipped``.
        Returned in reverse line order (highest main line first).
        """
        repo_relative = self._to_repo_relative(file_path)
        strict_lines = diff_lines.strict_new_lines.get(repo_relative, set())
        if not diff_lines.old_lines.get(repo_relative) or not strict_lines:
            return []

        # Active duplications (FR-5) of the branch, from the temp project.
        groups, target_ref = await self._fetch_duplications(file_path.as_posix(), temp_project, file_path, verbose)
        active_ranges: set[tuple[int, int]] = set()
        if target_ref is not None:
            for group in select_in_scope_duplications(groups, target_ref, strict_lines).in_scope:
                block = group.get_target_block(target_ref)
                if block is not None:
                    active_ranges.add((block.from_line, block.to_line))

        diag = FileDiagnostics(file_path=repo_relative, lookup_key=repo_relative)
        resolved_list = await get_resolved_duplications(
            self.config,
            repo_relative,
            {repo_relative: strict_lines},
            diff_lines.old_lines,
            active_ranges,
            original_project_key,
            diag,
        )
        if verbose:
            dim(f"  {file_path}: main duplications lookup {diag.resolved_dup_api_status}, {len(resolved_list)} found")

        tasks: list[MainDuplicationTask] = []
        for resolved in sorted(resolved_list, key=lambda r: r.main_from_line, reverse=True):
            task = build_main_duplication_task(
                self.branch_analyzer.repo, merge_base, file_path, repo_relative, resolved
            )
            if task is None:
                if verbose:
                    dim(
                        f"  {file_path}: main block at line {resolved.main_from_line} skipped (no main-side text or diff)"
                    )
                result.main_duplications_skipped += 1
                continue
            tasks.append(task)
        return tasks

    async def _fix_main_duplication(
        self,
        task: MainDuplicationTask,
        result: FileCleanupPrResult,
        diff_files: set[str],
        external_files: list[Path],
        dry_run: bool,
        merge_base: str,
    ) -> int:
        """Fix one main duplication with one AI task and, on success, one commit (FR-6 Fix/Commits).

        The task's branch hunk/range was snapshotted before the first fix commit, so it is
        re-read from HEAD right before the AI call: an earlier commit in this file may have
        already rewritten it, which would otherwise leave the prompt pointing at stale lines.
        When no branch diff remains for the block (an earlier fix absorbed it), the task is
        skipped as already handled.

        Returns:
            The number of commits created (0 or 1).
        """
        line = task.resolved.main_from_line
        if dry_run:
            dim(f"  Would fix main duplication at main line {line} in {task.file_path} (dry-run)")
            result.main_duplications_fixed += 1
            return 0

        # Same failure policy as cleanup: a dirty tree left by a failed attempt raises here.
        self.git_manager.require_clean_working_directory()

        # Rebuild lazily so the branch diff is re-read from the current HEAD instead of the
        # pre-fix snapshot (stale for the 2nd+ group in a file).
        fresh_task = build_main_duplication_task(
            self.branch_analyzer.repo, merge_base, task.file_path, task.repo_relative, task.resolved
        )
        if fresh_task is None:
            dim(f"  Skipping main duplication at main line {line}: no branch diff remains after earlier fixes")
            result.main_duplications_skipped += 1
            return 0

        head_before = self.branch_analyzer.get_head_sha()
        dim(f"\n{task.file_path}: fixing main duplication at main line {line}")
        fix_result = await self.ai_tool.fix_duplication(
            build_main_duplication_prompt(fresh_task), task.file_path.as_posix()
        )
        if not fix_result.success:
            error(
                f"  Failed to fix main duplication at main line {line}: {fix_result.error_message or 'unknown error'}"
            )
            self._mark_failures(result, 1)
            return 0

        message = build_main_duplication_commit_message(fresh_task, self.ai_tool.tool_type.display_name)
        try:
            sha = self.git_manager.create_commit(message, None, include_untracked=True)
        except Exception as e:
            error(f"  Failed to commit main duplication fix: {e}")
            self._mark_failures(result, 1)
            return 0
        if sha is None:
            result.main_duplications_skipped += 1
            return 0
        result.main_duplications_fixed += 1
        self._record_external_files(head_before, diff_files, external_files)
        return 1

    async def _process_file(
        self,
        file_path: Path,
        result: FileCleanupPrResult,
        diff_lines: DiffLines,
        diff_files: set[str],
        external_files: list[Path],
        temp_project: TempProjectMetadata,
        min_severity: str | None,
        dry_run: bool,
        verbose: bool,
    ) -> int:
        """Fix one file's in-scope duplications, then its in-scope issues.

        Updates ``result`` and ``external_files`` in place.

        Returns:
            The number of in-scope items found in this file this round (0 means converged).
        """
        repo_relative = self._to_repo_relative(file_path)
        if repo_relative in self._stale_files:
            # An earlier commit this round (e.g. a duplication refactor extracting into this file)
            # changed it after the analysis, so its findings' line numbers no longer match.
            if verbose:
                dim(f"  {file_path}: deferred to next iteration (modified by an earlier fix this round)")
            return 1
        # DiffParser maps are keyed by repo-relative path; SonarQube is queried by CWD-relative path.
        new_lines = diff_lines.new_lines.get(repo_relative, set())
        strict_lines = diff_lines.strict_new_lines.get(repo_relative, set())
        if not new_lines and not strict_lines:
            if verbose:
                dim(f"  {file_path}: skipped (no changed lines)")
            return 0

        # Duplications first (FR-5): a refactor can remove or move code that has issues.
        dup_found, dup_failed, dup_commits = await self._fix_duplications(
            file_path, result, strict_lines, diff_files, external_files, temp_project, dry_run, verbose
        )
        if dup_commits:
            # The temp-project analysis predates these commits, so its line numbers are stale for
            # this file; defer its issues to the next iteration's fresh analysis.
            if verbose:
                dim(f"  {file_path}: issues deferred to next iteration (duplication commits shifted lines)")
            self._mark_failures(result, dup_failed)
            return dup_found

        # Issues second (FR-4).
        issue_found, issue_failed = await self._fix_issues(
            file_path, result, new_lines, strict_lines, min_severity, dry_run, verbose
        )
        self._mark_failures(result, dup_failed + issue_failed)
        return dup_found + issue_found

    @staticmethod
    def _mark_failures(result: FileCleanupPrResult, failed: int) -> None:
        """Flag the file result when any fix failed."""
        if failed:
            result.success = False
            result.error_message = f"{failed} fix(es) failed"

    async def _fix_duplications(
        self,
        file_path: Path,
        result: FileCleanupPrResult,
        strict_lines: set[int],
        diff_files: set[str],
        external_files: list[Path],
        temp_project: TempProjectMetadata,
        dry_run: bool,
        verbose: bool,
    ) -> tuple[int, int, int]:
        """Fix a file's in-scope duplications (FR-5).

        Returns:
            ``(in_scope_found, failed, commits_created)``.
        """
        cwd_relative = file_path.as_posix()
        groups, target_ref = await self._fetch_duplications(cwd_relative, temp_project, file_path, verbose)
        if target_ref is None:
            return 0, 0, 0

        scope = select_in_scope_duplications(groups, target_ref, strict_lines)
        result.duplications_out_of_scope = scope.out_of_scope_count
        if not scope.in_scope:
            return 0, 0, 0

        # Scoping ran once above; the hook only checks membership of the precomputed target ranges.
        in_scope_ranges = {_target_range(group, target_ref) for group in scope.in_scope}
        dim(f"\n{file_path}: {len(scope.in_scope)} in-scope duplication group(s)")
        head_before = self.branch_analyzer.get_head_sha()
        dedupe = DeduplicationOrchestrator(self.config, self.ai_tool, git_manager=self.git_manager)
        summary = await dedupe.dedupe_file(
            file_path=cwd_relative,
            dry_run=dry_run,
            max_duplications=None,
            group_filter=lambda group, ref: _target_range(group, ref) in in_scope_ranges,
        )
        result.duplications_fixed += summary.fixed
        if summary.commits:
            self._record_external_files(head_before, diff_files, external_files)
        return len(scope.in_scope), summary.failed, len(summary.commits)

    async def _fix_issues(
        self,
        file_path: Path,
        result: FileCleanupPrResult,
        new_lines: set[int],
        strict_lines: set[int],
        min_severity: str | None,
        dry_run: bool,
        verbose: bool,
    ) -> tuple[int, int]:
        """Fix a file's in-scope issues (FR-4).

        Returns:
            ``(in_scope_found, failed)``.
        """
        cwd_relative = file_path.as_posix()
        try:
            issues = await self.client.get_issues_for_file(cwd_relative)
        except ComponentNotFoundError:
            if verbose:
                dim(f"  {file_path}: skipped (not in SonarQube analysis)")
            return 0, 0

        scope = select_in_scope_issues(issues, new_lines, strict_lines)
        result.issues_out_of_scope = scope.out_of_scope_count
        actionable = IssueProcessor(min_severity=min_severity, max_issues=None).process(scope.in_scope)
        if not actionable.issues_to_fix:
            return 0, 0

        # Scoping ran once above; the hook only checks membership of the precomputed issue keys.
        in_scope_keys = {issue.key for issue in scope.in_scope}
        dim(f"\n{file_path}: {len(actionable.issues_to_fix)} in-scope issue(s)")
        fixer = VibeHealOrchestrator(config=self.config, ai_tool=self.ai_tool)
        summary = await fixer.fix_file(
            file_path=cwd_relative,
            dry_run=dry_run,
            max_issues=None,
            min_severity=min_severity,
            issue_filter=lambda issue: issue.key in in_scope_keys,
        )
        result.issues_fixed += summary.fixed
        return len(actionable.issues_to_fix), summary.failed

    async def _fetch_duplications(
        self,
        cwd_relative: str,
        temp_project: TempProjectMetadata,
        file_path: Path,
        verbose: bool,
    ) -> tuple[list[DuplicationGroup], str | None]:
        """Fetch a file's duplication groups from the temp project.

        Returns:
            ``(groups, target_ref)``; ``target_ref`` is None when the file is not in the
            analysis or has no target reference (nothing to scope).
        """
        try:
            async with DuplicationClient(self.config) as dup_client:
                response = await dup_client.get_duplications_for_file(cwd_relative)
        except ComponentNotFoundError:
            if verbose:
                dim(f"  {file_path}: skipped for duplications (not in SonarQube analysis)")
            return [], None
        if not response.duplications:
            return [], None
        target_ref = response.get_target_file_ref(f"{temp_project.project_key}:{cwd_relative}")
        return response.duplications, target_ref

    def _record_external_files(self, head_before: str, diff_files: set[str], external_files: list[Path]) -> None:
        """Record files a duplication refactor modified outside the branch diff (11.A-11)."""
        head_after = self.branch_analyzer.get_head_sha()
        if head_after == head_before:
            return
        changed = self.branch_analyzer.repo.git.diff("--name-only", head_before, head_after)
        for name in changed.splitlines():
            name = name.strip()
            if name:
                self._stale_files.add(name)
            if name and name not in diff_files and Path(name) not in external_files:
                warn(f"  Duplication refactor modified a file outside the branch diff: {name}")
                external_files.append(Path(name))

    def _to_repo_relative(self, file_path: Path) -> str:
        """Convert a (possibly CWD-relative) path to a repo-root-relative POSIX string."""
        return to_repo_relative(file_path, Path(self.branch_analyzer.repo.working_dir))
