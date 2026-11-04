"""Branch cleanup orchestration."""

import asyncio
from pathlib import Path

from pydantic import BaseModel

from dotharness.vibe_heal.ai_tools.base import AITool
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.file_selection import select_modified_files
from dotharness.vibe_heal.git.manager import GitManager
from dotharness.vibe_heal.orchestrator import VibeHealOrchestrator
from dotharness.vibe_heal.output import bold, dim, error, success, warn
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisResult, AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import ComponentNotFoundError
from dotharness.vibe_heal.sonarqube.project_manager import ProjectManager, TempProjectMetadata


class FileCleanupResult(BaseModel):
    """Result of cleaning up a single file."""

    file_path: Path
    issues_fixed: int
    success: bool
    error_message: str | None = None


class CleanupResult(BaseModel):
    """Result of branch cleanup operation."""

    success: bool
    files_processed: list[FileCleanupResult]
    temp_project: TempProjectMetadata | None = None
    analysis_result: AnalysisResult | None = None
    total_issues_fixed: int = 0
    error_message: str | None = None


class CleanupOrchestrator:
    """Orchestrates branch cleanup workflow.

    Coordinates:
    - Branch analysis (modified files)
    - Temporary project creation
    - SonarQube analysis
    - Issue fixing via AI tool
    - Project cleanup
    """

    def __init__(
        self,
        config: VibeHealConfig,
        client: SonarQubeClient,
        ai_tool: AITool,
    ) -> None:
        """Initialize the cleanup orchestrator.

        Args:
            config: Application configuration
            client: SonarQube API client
            ai_tool: AI tool for fixing issues
        """
        self.config = config
        self.client = client
        self.ai_tool = ai_tool
        self.project_manager = ProjectManager(client)
        self.analysis_runner = AnalysisRunner(config, client)
        self.branch_analyzer = BranchAnalyzer(Path.cwd())
        self.git_manager = GitManager(Path.cwd(), pre_commit_command=config.pre_commit_command)

    async def cleanup_branch(
        self,
        base_branch: str = "origin/main",
        max_iterations: int = 10,
        file_patterns: list[str] | None = None,
        verbose: bool = False,
    ) -> CleanupResult:
        """Clean up all modified files in current branch.

        Workflow:
        1. Analyze branch to get modified files
        2. Create temporary SonarQube project
        3. Loop (max_iterations times):
           - Run full repo analysis
           - Get issues for all modified files
           - If no issues, break
           - Fix all modified files (one pass)
        4. Delete temporary project

        Args:
            base_branch: Base branch to compare against (default: origin/main)
            max_iterations: Maximum analysis iterations for the entire branch (default: 10)
            file_patterns: Optional list of glob patterns to filter files
            verbose: Enable verbose output

        Returns:
            CleanupResult with success status and details
        """
        temp_project: TempProjectMetadata | None = None
        files_processed: list[FileCleanupResult] = []

        try:
            # Step 1: Validate and filter modified files
            modified_files = select_modified_files(self.branch_analyzer, base_branch, file_patterns)

            if not modified_files:
                return CleanupResult(
                    success=True,
                    files_processed=[],
                    total_issues_fixed=0,
                )

            # Step 2: Create temporary SonarQube project
            temp_project = await self._create_temp_project()

            # Step 3: Iterative fix loop
            analysis_result: AnalysisResult | None = None
            file_results_map: dict[Path, FileCleanupResult] = {
                f: FileCleanupResult(file_path=f, issues_fixed=0, success=True) for f in modified_files
            }

            for iteration in range(max_iterations):
                bold(f"\nIteration {iteration + 1}/{max_iterations}")

                # Run full repo analysis
                dim("Running SonarQube analysis on full repository...")
                analysis_result = await self.analysis_runner.run_analysis(
                    project_key=temp_project.project_key,
                    project_name=temp_project.project_name,
                    project_dir=Path.cwd(),
                )

                if not analysis_result.success:
                    error(f"Analysis failed: {analysis_result.error_message}")
                    return CleanupResult(
                        success=False,
                        files_processed=list(file_results_map.values()),
                        temp_project=temp_project,
                        analysis_result=analysis_result,
                        error_message=f"Analysis failed at iteration {iteration + 1}: {analysis_result.error_message}",
                    )

                dim(f"Analysis completed. Dashboard: {analysis_result.dashboard_url}")

                # Check for fixable issues in modified files
                total_fixable_issues, files_with_issues = await self._check_files_for_issues(
                    modified_files, temp_project, verbose
                )

                if total_fixable_issues == 0:
                    success("✓ No fixable issues remaining!")
                    break

                dim(f"Total fixable issues across all files: {total_fixable_issues}")

                # Fix all files with issues
                await self._fix_files_with_issues(files_with_issues, file_results_map, temp_project, iteration)

                # Wait before next iteration to let SonarQube process commits
                if iteration < max_iterations - 1:
                    dim("Waiting for SonarQube to process changes...")
                    await asyncio.sleep(5)

            # Calculate results
            files_processed = list(file_results_map.values())
            total_issues_fixed = sum(f.issues_fixed for f in files_processed)

            return CleanupResult(
                success=True,
                files_processed=files_processed,
                temp_project=temp_project,
                analysis_result=analysis_result,
                total_issues_fixed=total_issues_fixed,
            )

        except Exception as e:
            return CleanupResult(
                success=False,
                files_processed=files_processed,
                temp_project=temp_project,
                error_message=f"Cleanup failed: {e}",
            )

        finally:
            # Always cleanup temporary project
            await self._cleanup_temp_project(temp_project)

    async def _create_temp_project(self) -> TempProjectMetadata:
        """Create temporary SonarQube project for analysis.

        Returns:
            TempProjectMetadata for the created project
        """
        current_branch = self.branch_analyzer.get_current_branch()
        user_email = self.branch_analyzer.get_user_email()

        return await self.project_manager.create_temp_project_with_settings(
            base_key=self.config.sonarqube_project_key,
            branch_name=current_branch,
            user_email=user_email,
            command_name="cleanup",
        )

    async def _check_files_for_issues(
        self,
        modified_files: list[Path],
        temp_project: TempProjectMetadata,
        verbose: bool,
    ) -> tuple[int, list[Path]]:
        """Check modified files for fixable issues.

        Args:
            modified_files: List of modified files to check
            temp_project: Temporary project metadata
            verbose: Enable verbose output

        Returns:
            Tuple of (total_fixable_issues, files_with_issues)
        """
        # Override project key to use temp project
        original_project_key = self.config.sonarqube_project_key
        self.config.sonarqube_project_key = temp_project.project_key
        self.client.config.sonarqube_project_key = temp_project.project_key

        try:
            total_fixable_issues = 0
            files_with_issues: list[Path] = []

            for file_path in modified_files:
                try:
                    issues = await self.client.get_issues_for_file(str(file_path), resolved=False)
                    fixable_issues = [issue for issue in issues if issue.is_fixable]

                    if verbose and issues:
                        dim(f"  {file_path}: {len(issues)} total, {len(fixable_issues)} fixable")

                    if fixable_issues:
                        files_with_issues.append(file_path)
                        total_fixable_issues += len(fixable_issues)
                        dim(f"  {file_path}: {len(fixable_issues)} fixable issues")
                except ComponentNotFoundError:
                    if verbose:
                        dim(f"  {file_path}: skipped (not in SonarQube analysis)")

            return total_fixable_issues, files_with_issues

        finally:
            # Restore original project key
            self.config.sonarqube_project_key = original_project_key
            self.client.config.sonarqube_project_key = original_project_key

    async def _fix_files_with_issues(
        self,
        files_with_issues: list[Path],
        file_results_map: dict[Path, FileCleanupResult],
        temp_project: TempProjectMetadata,
        iteration: int,
    ) -> None:
        """Fix all files with issues.

        Args:
            files_with_issues: List of files that have issues
            file_results_map: Map of file paths to cleanup results (updated in place)
            temp_project: Temporary project metadata
            iteration: Current iteration number
        """
        # Override project key to use temp project
        original_project_key = self.config.sonarqube_project_key
        self.config.sonarqube_project_key = temp_project.project_key
        self.client.config.sonarqube_project_key = temp_project.project_key

        try:
            for file_path in files_with_issues:
                dim(f"\nFixing {file_path}...")

                # Get issues for this file
                issues = await self.client.get_issues_for_file(str(file_path), resolved=False)
                fixable_issues = [issue for issue in issues if issue.is_fixable]

                # Fix file using existing orchestrator
                orchestrator = VibeHealOrchestrator(config=self.config)

                fix_summary = await orchestrator.fix_file(
                    file_path=str(file_path),
                    max_issues=len(fixable_issues),
                    min_severity=None,
                    dry_run=False,
                )

                dim(f"  Fixed: {fix_summary.fixed}, Failed: {fix_summary.failed}, Skipped: {fix_summary.skipped}")

                # Update file results
                current_result = file_results_map[file_path]
                file_results_map[file_path] = FileCleanupResult(
                    file_path=file_path,
                    issues_fixed=current_result.issues_fixed + fix_summary.fixed,
                    success=current_result.success and not fix_summary.has_failures,
                    error_message=f"Fixes failed at iteration {iteration + 1}" if fix_summary.has_failures else None,
                )

        finally:
            # Restore original project key
            self.config.sonarqube_project_key = original_project_key
            self.client.config.sonarqube_project_key = original_project_key

    async def _cleanup_temp_project(self, temp_project: TempProjectMetadata | None) -> None:
        """Clean up temporary SonarQube project.

        Args:
            temp_project: Temporary project metadata (None if not created)
        """
        if temp_project:
            try:
                dim(f"Deleting temporary project: {temp_project.project_key}")
                await self.project_manager.delete_project(temp_project.project_key)
                success("✓ Temporary project deleted")
            except Exception as e:
                warn(f"Warning: Failed to delete temporary project: {e}")
