"""Review orchestration for analyzing SonarQube issues on changed lines."""

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.deduplication.client import DuplicationClient
from dotharness.vibe_heal.deduplication.models import DuplicationGroup, DuplicationsResponse
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer
from dotharness.vibe_heal.git.diff_parser import DiffParser
from dotharness.vibe_heal.git.file_selection import filter_files_by_patterns
from dotharness.vibe_heal.git.paths import to_repo_relative
from dotharness.vibe_heal.output import console, dim, error, success, warn
from dotharness.vibe_heal.review.duplication_scope import (
    build_other_locations,
    changed_lines_in_block,
    get_resolved_duplications,
)
from dotharness.vibe_heal.review.github import GitHubReviewClient
from dotharness.vibe_heal.review.line_filter import IssueLineFilter
from dotharness.vibe_heal.review.models import (
    FileDiagnostics,
    FileReview,
    ResolvedDuplication,
    ReviewDuplication,
    ReviewIssue,
    ReviewResult,
)
from dotharness.vibe_heal.review.reporter import (
    default_report_dir,
    load_report_from_path,
    write_report_to_file,
)
from dotharness.vibe_heal.sonarqube.analysis_runner import AnalysisRunner
from dotharness.vibe_heal.sonarqube.client import SonarQubeClient
from dotharness.vibe_heal.sonarqube.exceptions import (
    ComponentNotFoundError,
    SonarQubeAPIError,
    SonarQubeError,
    SonarQubeRuleNotFoundError,
)
from dotharness.vibe_heal.sonarqube.models import SonarQubeRule
from dotharness.vibe_heal.sonarqube.project_manager import ProjectManager, TempProjectMetadata

logger = logging.getLogger(__name__)

DEFAULT_BASE_BRANCH = "origin/main"


class ReviewAnalysisResult(BaseModel):
    """Result of a review analysis operation."""

    success: bool = True
    project_key: str = ""
    project_label: str = ""
    branch: str = ""
    head_sha: str = ""
    base_branch: str = ""
    files: list[FileReview] = Field(default_factory=list)
    files_analyzed: int = 0
    diagnostics: list[FileDiagnostics] = Field(default_factory=list)
    diff_files_found: int = 0
    diff_map_keys: list[str] = Field(default_factory=list)
    diff_output_sample: str = ""
    error_message: str | None = None
    report_file: Path | None = None

    @property
    def total_issues(self) -> int:
        """Return the total number of issues across all files."""
        return sum(len(f.issues) for f in self.files)

    @property
    def total_duplications(self) -> int:
        """Return the total number of active and resolved duplication findings."""
        return sum(len(f.duplications) + len(f.resolved_duplications) for f in self.files)


class BaselineScanResult(BaseModel):
    """Result of a baseline scan operation."""

    success: bool
    project_key: str
    dashboard_url: str | None = None
    error_message: str | None = None


class ReviewOrchestrator:
    """Orchestrates the review workflow: analyze changed lines for SonarQube issues.

    Coordinates:
    - Branch analysis (modified files)
    - Diff parsing (changed lines)
    - Temporary project creation
    - SonarQube analysis
    - Issue filtering to changed lines
    - Report generation
    - GitHub review posting
    """

    def __init__(
        self,
        config: VibeHealConfig,
        client: SonarQubeClient,
        branch_analyzer: BranchAnalyzer | None = None,
        diff_parser: DiffParser | None = None,
    ) -> None:
        """Initialize the review orchestrator.

        Args:
            config: Application configuration.
            client: SonarQube API client.
            branch_analyzer: Optional BranchAnalyzer instance (for testing).
            diff_parser: Optional DiffParser instance (for testing).
        """
        self.config = config
        self.client = client
        self.branch_analyzer = branch_analyzer if branch_analyzer is not None else BranchAnalyzer(Path.cwd())
        self.diff_parser = diff_parser if diff_parser is not None else DiffParser(Path.cwd())
        self.project_manager = ProjectManager(client)
        self.analysis_runner = AnalysisRunner(config, client)
        self.github_client = GitHubReviewClient()
        self._rule_cache: dict[str, SonarQubeRule | None] = {}

    async def _resolve_base_branch(self, base_branch: str | None, verbose: bool) -> str:
        """Resolve the base branch to diff against.

        Returns base_branch unchanged if the caller passed one explicitly
        (no gh call is made in that case — explicit intent always wins).
        Otherwise, attempts to auto-detect the real PR base via
        GitHubReviewClient.detect_pr_base_branch(), prefixed with "origin/"
        to match this codebase's remote-ref convention. Falls back to
        DEFAULT_BASE_BRANCH when no open PR is found (or gh is unavailable),
        preserving the pre-existing behavior for that case.
        """
        if base_branch is not None:
            return base_branch

        detected = await self.github_client.detect_pr_base_branch()
        if detected is None:
            if verbose:
                dim(f"No open PR found; using default base branch: {DEFAULT_BASE_BRANCH}")
            return DEFAULT_BASE_BRANCH

        resolved = f"origin/{detected}"
        if verbose:
            dim(f"Auto-detected PR base branch: {resolved}")
        return resolved

    async def run_analysis(
        self,
        base_branch: str | None = None,
        file_patterns: list[str] | None = None,
        report_file: Path | None = None,
        verbose: bool = False,
        coverage: bool = False,
    ) -> ReviewAnalysisResult:
        """Analyze SonarQube issues on changed lines.

        Workflow:
        1. Get modified files from BranchAnalyzer
        2. Filter by patterns if specified
        3. Get changed lines from DiffParser
        4. Create temp project, copy exclusion settings
        5. Run analysis (sonar-scanner)
        6. For each modified file: fetch issues, filter to changed lines
        7. Build ReviewResult, write reports
        8. Delete temp project in finally block

        Args:
            base_branch: Base branch to compare against. When None (the
                default), auto-detects the current branch's open PR base via
                gh and diffs against that; falls back to "origin/main" if no
                PR is found. Passing an explicit value skips detection.
            file_patterns: Optional list of glob patterns to filter files.
            report_file: Optional path to write the report JSON to.
                If provided, the parent directory is used for report output.
            verbose: Enable verbose output.
            coverage: When True, fetch line coverage data for changed lines and
                populate coverage_pct/covered_lines/instrumented_changed_lines on
                each FileReview. Files with coverage data are included even if they
                have no issues or duplications.

        Returns:
            ReviewAnalysisResult with success status and review data.
        """
        temp_project: TempProjectMetadata | None = None
        base_branch = await self._resolve_base_branch(base_branch, verbose)
        branch = self.branch_analyzer.get_current_branch()
        head_sha = self.branch_analyzer.get_head_sha()
        if report_file is None:
            report_file = default_report_dir(self.config.sonarqube_project_key, branch) / "review.json"
        result = ReviewAnalysisResult(
            project_key=self.config.sonarqube_project_key,
            branch=branch,
            head_sha=head_sha,
            base_branch=base_branch,
            report_file=report_file,
        )

        try:
            # Step 1: Get modified files
            dim(f"Analyzing branch against {base_branch}...")
            modified_files = self.branch_analyzer.get_modified_files(base_branch)
            dim(f"Found {len(modified_files)} modified files")

            if not modified_files:
                success("No modified files to review.")
                self._write_report(result, report_file)
                return result

            # Step 2: Filter by patterns if specified
            if file_patterns:
                dim(f"Filtering files with patterns: {file_patterns}")
                modified_files = filter_files_by_patterns(modified_files, file_patterns)
                dim(f"After filtering: {len(modified_files)} files remain")

            if not modified_files:
                success("No files match the specified patterns.")
                self._write_report(result, report_file)
                return result

            # Step 3: Get changed lines (both new-side and old-side)
            diff_lines = self.diff_parser.get_diff_lines(base_branch)
            changed_lines_map = diff_lines.new_lines
            old_changed_lines_map = diff_lines.old_lines
            strict_new_lines_map = diff_lines.strict_new_lines
            result.diff_files_found = len(changed_lines_map)
            result.diff_map_keys = sorted(changed_lines_map.keys())
            raw_diff = self.diff_parser.get_raw_diff(base_branch)
            result.diff_output_sample = raw_diff[:500]

            # Step 4: Create temp project
            temp_project = await self._create_temp_project()
            result.project_label = temp_project.project_name

            # Step 5: Run analysis
            console.print("[dim]Running SonarQube analysis...[/dim]")
            analysis_result = await self.analysis_runner.run_analysis(
                project_key=temp_project.project_key,
                project_name=temp_project.project_name,
                project_dir=Path.cwd(),
            )

            if not analysis_result.success:
                error_msg = analysis_result.error_message or "Analysis failed"
                error(f"Analysis failed: {error_msg}")
                result.success = False
                result.error_message = error_msg
                result.report_file = None
                return result

            console.print("[dim]Analysis completed successfully.[/dim]")

            # Step 6: Fetch issues and duplications for each file, filter to changed lines
            result.files_analyzed = len(modified_files)
            original_project_key = self.config.sonarqube_project_key
            temp_key = temp_project.project_key
            for file_path in modified_files:
                # repo_relative is the POSIX repo-root-relative path used for:
                # - DiffParser map lookups (keys are always repo-root-relative POSIX)
                # - FileReview.file_path (GitHub requires repo-root-relative paths)
                repo_relative = self._to_repo_relative(file_path)
                file_issues, diag = await self._get_filtered_issues(
                    file_path,
                    changed_lines_map,
                    verbose,
                    project_key=temp_key,
                    strict_changed_lines_map=strict_new_lines_map,
                )
                await self._enrich_issues_with_descriptions(file_issues)
                active_dups = await self._get_active_duplications(
                    file_path, strict_new_lines_map, diag, project_key=temp_key
                )
                active_ranges = {(d.from_line, d.to_line) for d in active_dups}
                resolved_dups = await self._get_resolved_duplications(
                    file_path, changed_lines_map, old_changed_lines_map, active_ranges, original_project_key, diag
                )
                coverage_pct, covered_lines, instrumented_changed_lines = await self._fetch_coverage(
                    repo_relative, changed_lines_map, temp_key, coverage, verbose
                )
                result.diagnostics.append(diag)
                if file_issues or active_dups or resolved_dups or coverage_pct is not None:
                    result.files.append(
                        FileReview(
                            file_path=repo_relative,
                            issues=file_issues,
                            duplications=active_dups,
                            resolved_duplications=resolved_dups,
                            coverage_pct=coverage_pct,
                            covered_lines=covered_lines,
                            instrumented_changed_lines=instrumented_changed_lines,
                        )
                    )
                elif verbose:
                    dim(f"  {file_path}: no issues on changed lines")

            # Step 7: Write reports
            self._write_report(result, report_file)

            success(
                f"Review complete: {result.total_issues} issue(s), "
                f"{result.total_duplications} duplication finding(s) "
                f"across {len(result.files)} file(s)."
            )

        except Exception as e:
            result.success = False
            result.error_message = f"Review failed: {e}"
            result.report_file = None
            return result

        finally:
            await self._cleanup_temp_project(temp_project)

        return result

    async def run_baseline_scan(self) -> BaselineScanResult:
        """Run a full SonarQube scan directly against the real project.

        Refreshes the real project's SonarQube analysis (no temp project, no
        diff, no per-file report) so later PR reviews have an up-to-date
        baseline for duplicate-code comparisons.

        Returns:
            BaselineScanResult with success status and dashboard URL or error.
        """
        project_key = self.config.sonarqube_project_key
        try:
            dim(f"Running baseline SonarQube scan against {project_key}...")
            analysis_result = await self.analysis_runner.run_analysis(
                project_key=project_key,
                project_name=project_key,
                project_dir=Path.cwd(),
            )
            if not analysis_result.success:
                error_msg = analysis_result.error_message or "Analysis failed"
                return BaselineScanResult(success=False, project_key=project_key, error_message=error_msg)
            return BaselineScanResult(
                success=True,
                project_key=project_key,
                dashboard_url=analysis_result.dashboard_url,
            )
        except Exception as e:
            return BaselineScanResult(
                success=False,
                project_key=project_key,
                error_message=f"Baseline scan failed: {e}",
            )

    async def run_post(
        self,
        report_file: Path,
        pr_number: int | None = None,
        verbose: bool = False,
    ) -> None:
        """Post review comments to a GitHub PR.

        Workflow:
        1. Load report from JSON
        2. Detect or use explicit PR number
        3. Post review comments via GitHubReviewClient

        Args:
            report_file: Path to the saved review.json file.
            pr_number: Optional explicit PR number. If None, auto-detect.
            verbose: Enable verbose output.
        """
        # Step 1: Load report
        dim(f"Loading report from {report_file}...")
        report = load_report_from_path(report_file)
        if verbose:
            dim(f"  Report: branch={report.branch}, {report.total_issues} issue(s), {len(report.files)} file(s)")

        # Step 2: Detect PR number
        if pr_number is not None:
            pr = pr_number
            if verbose:
                dim(f"  Using explicit PR #{pr}")
        else:
            pr = await self.github_client.detect_pr()
            if verbose:
                dim(f"  Auto-detected PR #{pr}")

        # Step 3: Post review
        await self.github_client.post_review(pr, report)
        success(
            f"Posted {report.total_issues} issue(s) and "
            f"{report.total_duplications} duplication finding(s) "
            f"as review comments on PR #{pr}."
        )

    async def _enrich_issues_with_descriptions(self, issues: list[ReviewIssue]) -> None:
        """Populate root_cause on each issue by fetching rule details from SonarQube.

        Mutates issues in-place. Caches results per rule key for the lifetime of
        this orchestrator instance. No-ops when config.include_rule_description is False.

        Args:
            issues: Issues to enrich in-place.
        """
        if not self.config.include_rule_description:
            return

        for issue in issues:
            if issue.rule not in self._rule_cache:
                try:
                    self._rule_cache[issue.rule] = await self.client.get_rule_details(issue.rule)
                except SonarQubeRuleNotFoundError:
                    logger.debug("Rule %s not found in SonarQube (external rule); skipping root_cause", issue.rule)
                    self._rule_cache[issue.rule] = None
                except SonarQubeError as e:
                    logger.warning("Could not fetch rule details for %s: %s", issue.rule, e)
                    self._rule_cache[issue.rule] = None

            cached = self._rule_cache[issue.rule]
            if cached is not None:
                issue.root_cause = cached.root_cause_html

    async def _create_temp_project(self) -> TempProjectMetadata:
        """Create temporary SonarQube project for analysis.

        Returns:
            TempProjectMetadata for the created project.
        """
        console.print("[dim]Creating temporary SonarQube project...[/dim]")
        current_branch = self.branch_analyzer.get_current_branch()
        user_email = self.branch_analyzer.get_user_email()

        temp_project = await self.project_manager.create_temp_project(
            base_key=self.config.sonarqube_project_key,
            branch_name=current_branch,
            user_email=user_email,
            command_name="review",
        )
        dim(f"Created project: {temp_project.project_key}")

        try:
            copied, inherited_count, failed_count = await self.project_manager.copy_exclusion_settings(
                source_key=self.config.sonarqube_project_key,
                target_key=temp_project.project_key,
            )
            if copied:
                dim(f"Copied {len(copied)} exclusion setting(s): {', '.join(copied)}")
            if inherited_count:
                dim(f"Skipped {inherited_count} inherited setting(s)")
            if failed_count:
                warn(f"{failed_count} exclusion setting(s) failed to apply")
            if not copied and not inherited_count and not failed_count:
                dim("No exclusion settings configured on source project")
        except Exception as e:
            warn(f"Warning: Could not copy exclusion settings: {e}")

        return temp_project

    async def _get_filtered_issues(
        self,
        file_path: Path,
        changed_lines_map: dict[str, set[int]],
        verbose: bool,
        project_key: str,
        strict_changed_lines_map: dict[str, set[int]] | None = None,
    ) -> tuple[list[ReviewIssue], FileDiagnostics]:
        """Fetch issues for a file and filter to changed lines.

        Args:
            file_path: Path to the file.
            changed_lines_map: Mapping of file paths to changed line sets
                (may include trailing context lines from DiffParser).
            verbose: Enable verbose output.
            project_key: SonarQube project key to query.
            strict_changed_lines_map: Unexpanded changed lines (no trailing
                context). Issues only in the trailing window are included but
                marked on_changed_line=False so they are not posted inline.

        Returns:
            Tuple of (filtered ReviewIssues, FileDiagnostics for this file).
        """
        file_path_str = file_path.as_posix()
        repo_relative = self._to_repo_relative(file_path)
        changed_lines = changed_lines_map.get(repo_relative, set())

        diag = FileDiagnostics(
            file_path=file_path_str,
            lookup_key=repo_relative,
            changed_lines=sorted(changed_lines),
        )

        try:
            issues = await self.client.get_issues(component=f"{project_key}:{file_path_str}", resolved=False)
        except ComponentNotFoundError:
            if verbose:
                dim(f"  {file_path}: skipped (not in SonarQube analysis)")
            return [], diag

        diag.sonar_issue_count = len(issues)
        diag.sonar_issue_lines = sorted(i.line for i in issues if i.line is not None)

        if not changed_lines:
            if verbose:
                dim(f"  {file_path}: no changed lines in diff")
            return [], diag

        strict_lines = strict_changed_lines_map.get(repo_relative) if strict_changed_lines_map else None
        filtered = IssueLineFilter.filter_issues(issues, changed_lines, strict_changed_lines=strict_lines)

        if verbose and filtered:
            dim(f"  {file_path}: {len(filtered)} issue(s) on changed lines")
        return filtered, diag

    async def _get_active_duplications(
        self,
        file_path: Path,
        changed_lines_map: dict[str, set[int]],
        diag: FileDiagnostics,
        project_key: str,
    ) -> list[ReviewDuplication]:
        """Fetch duplication blocks from the temp project that intersect changed lines.

        Args:
            file_path: Path to the file.
            changed_lines_map: New-side changed lines per file.
            project_key: SonarQube project key to query (the temp project key).
            diag: Per-file diagnostics object to populate with API outcome.

        Returns:
            ReviewDuplication entries for each overlapping block, if any.
        """
        repo_relative = self._to_repo_relative(file_path)
        new_changed_lines = changed_lines_map.get(repo_relative, set())
        if not new_changed_lines:
            diag.active_dup_api_status = "skipped_no_changed_lines"
            return []

        # The temp project is analyzed with project_dir=Path.cwd(), so component
        # paths are CWD-relative. Using repo_relative (repo-root-relative) would
        # miss files when running from a monorepo subdirectory.
        cwd_relative = file_path.as_posix()
        try:
            async with DuplicationClient(
                self.config.model_copy(update={"sonarqube_project_key": project_key})
            ) as dup_client:
                response: DuplicationsResponse = await dup_client.get_duplications_for_file(cwd_relative)
        except ComponentNotFoundError:
            diag.active_dup_api_status = "component_not_found"
            return []
        except SonarQubeAPIError as e:
            diag.active_dup_api_status = f"api_error:{e}"
            return []
        except Exception as e:
            diag.active_dup_api_status = f"error:{type(e).__name__}:{e}"
            return []

        diag.active_dup_api_status = "ok"
        diag.active_dup_groups_found = len(response.duplications)

        if not response.duplications:
            return []

        component_key = f"{project_key}:{cwd_relative}"
        target_ref = response.get_target_file_ref(component_key)
        if target_ref is None:
            return []

        diag.active_dup_target_ref_found = True

        findings: list[ReviewDuplication] = []
        for group in response.duplications:
            finding = self._build_active_finding(group, target_ref, new_changed_lines, response)
            if finding is not None:
                diag.active_dup_blocks_intersecting += 1
                findings.append(finding)
        return findings

    def _build_active_finding(
        self,
        group: DuplicationGroup,
        target_ref: str,
        new_changed_lines: set[int],
        response: DuplicationsResponse,
    ) -> ReviewDuplication | None:
        target_block = group.get_target_block(target_ref)
        if target_block is None:
            return None
        changed_in_block = changed_lines_in_block(target_block, new_changed_lines)
        if not changed_in_block:
            return None
        # Use the lowest changed line in the block as the anchor so the GitHub
        # PR comment is attached to a line that is actually in the diff.
        anchor_line = min(changed_in_block)
        other_locations = build_other_locations(group, target_block, response)
        return ReviewDuplication(
            from_line=target_block.from_line,
            to_line=target_block.to_line,
            anchor_line=anchor_line,
            other_locations=other_locations,
        )

    async def _get_resolved_duplications(
        self,
        file_path: Path,
        changed_lines_map: dict[str, set[int]],
        old_changed_lines_map: dict[str, set[int]],
        active_dup_ranges: set[tuple[int, int]],
        original_project_key: str,
        diag: FileDiagnostics,
    ) -> list[ResolvedDuplication]:
        """Warn about duplication blocks from main that were modified but not active in temp.

        Thin wrapper over :func:`get_resolved_duplications`.
        """
        return await get_resolved_duplications(
            self.config,
            self._to_repo_relative(file_path),
            changed_lines_map,
            old_changed_lines_map,
            active_dup_ranges,
            original_project_key,
            diag,
        )

    async def _fetch_coverage(
        self,
        repo_relative: str,
        changed_lines_map: dict[str, set[int]],
        project_key: str,
        coverage: bool,
        verbose: bool,
    ) -> tuple[float | None, int, int]:
        """Fetch line coverage for a file's changed lines from the temp project.

        Args:
            repo_relative: Repo-root-relative POSIX path to the file.
            changed_lines_map: Mapping of file paths to changed line sets.
            project_key: Temporary SonarQube project key.
            coverage: When False, returns (None, 0, 0) immediately without any API call.
            verbose: Enable verbose output.

        Returns:
            Tuple of (coverage_pct, covered_lines, instrumented_changed_lines).
            coverage_pct is None when coverage is False, data is unavailable, or fetch fails.
        """
        if not coverage:
            return None, 0, 0
        changed_lines_for_file = changed_lines_map.get(repo_relative, set())
        if not changed_lines_for_file:
            return None, 0, 0
        try:
            cov = await self.client.get_line_coverage(repo_relative, changed_lines_for_file, project_key=project_key)
        except Exception as exc:
            warn(f"  {repo_relative}: coverage fetch failed: {exc}")
            cov = None
        if cov is not None:
            covered_lines, instrumented_changed_lines = cov
            return round(covered_lines / instrumented_changed_lines * 100, 1), covered_lines, instrumented_changed_lines
        if verbose:
            dim(f"  {repo_relative}: no coverage data available")
        return None, 0, 0

    async def _cleanup_temp_project(self, temp_project: TempProjectMetadata | None) -> None:
        """Clean up temporary SonarQube project.

        Args:
            temp_project: Temporary project metadata (None if not created).
        """
        if temp_project:
            try:
                dim(f"Deleting temporary project: {temp_project.project_key}")
                await self.project_manager.delete_project(temp_project.project_key)
                success("Temporary project deleted.")
            except Exception as e:
                warn(f"Warning: Failed to delete temporary project: {e}")

    def _to_repo_relative(self, file_path: Path) -> str:
        """Convert a file path to a repo-root-relative POSIX string (see ``to_repo_relative``)."""
        return to_repo_relative(file_path, Path(self.branch_analyzer.repo.working_dir))

    def _write_report(self, result: ReviewAnalysisResult, report_file: Path | None) -> None:
        """Write report files if a report path is specified.

        Writes JSON to the exact report_file path and derives the markdown
        path from it (same stem, .md extension).

        Args:
            result: Review result to write.
            report_file: Optional path to the report JSON file.
        """
        if report_file is None:
            return

        review_result = ReviewResult(
            project_key=result.project_key,
            project_label=result.project_label,
            branch=result.branch,
            head_sha=result.head_sha,
            base_branch=result.base_branch,
            files=result.files,
            files_analyzed=result.files_analyzed,
            diagnostics=result.diagnostics,
            diff_files_found=result.diff_files_found,
            diff_map_keys=result.diff_map_keys,
            diff_output_sample=result.diff_output_sample,
        )

        write_report_to_file(review_result, report_file)
        md_path = report_file.parent / (report_file.stem + ".md")
        dim(f"Reports written to {report_file} and {md_path}")
