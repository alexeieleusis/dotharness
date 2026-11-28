"""Console presentation for the vibe_heal CLI: formats and prints command results."""

import sys

from rich.markup import escape as rich_escape
from rich.table import Table

from dotharness.vibe_heal.cleanup.orchestrator import CleanupResult
from dotharness.vibe_heal.cleanup_pr.orchestrator import CleanupPrResult, FileCleanupPrResult
from dotharness.vibe_heal.deduplication.orchestrator import DedupeBranchResult
from dotharness.vibe_heal.output import bold, bold_cyan, console, dim, error, success, warn
from dotharness.vibe_heal.review.models import ReviewIssue
from dotharness.vibe_heal.review.orchestrator import BaselineScanResult, ReviewAnalysisResult
from dotharness.vibe_heal.sonarqube.project_manager import StaleProject

GITHUB_FOOTER_LINE = "\n[dim]GitHub: https://github.com/alexeieleusis/dotharness[/dim]"
STATUS_SUCCESS = "[green]✓[/green]"
STATUS_FAILURE = "[red]✗[/red]"
PER_FILE_RESULTS_HEADER = "\n[bold]Per-File Results:[/bold]"


def display_branch_operation_header(
    operation_name: str,
    base_branch: str,
    max_iterations: int,
    file_patterns: list[str] | None,
    iterations_label: str = "Max iterations per file",
) -> None:
    """Display header for branch operations.

    Args:
        operation_name: Name of the operation (e.g., "Branch Cleanup")
        base_branch: Base branch being compared against
        max_iterations: Maximum iterations
        file_patterns: Optional file patterns to filter
        iterations_label: Label describing what max_iterations counts
    """
    bold_cyan(f"\n{operation_name}")
    console.print(f"  Base branch: {base_branch}")
    console.print(f"  {iterations_label}: {max_iterations}")
    if file_patterns:
        console.print(f"  File patterns: {', '.join(file_patterns)}")
    console.print()


def display_cleanup_results(result: CleanupResult) -> None:
    """Display cleanup results.

    Args:
        result: Cleanup result to display
    """
    console.print("\n[bold]Cleanup Summary:[/bold]")
    console.print(f"  Files processed: {len(result.files_processed)}")
    success(f"  Total issues fixed: {result.total_issues_fixed}")

    if result.files_processed:
        console.print(PER_FILE_RESULTS_HEADER)
        for file_result in result.files_processed:
            status = STATUS_SUCCESS if file_result.success else STATUS_FAILURE
            console.print(
                f"  {status} {rich_escape(str(file_result.file_path))}: {file_result.issues_fixed} issues fixed"
            )
            if file_result.error_message:
                error(f"      Error: {file_result.error_message}")

    if not result.success:
        if result.error_message:
            error(f"\nCleanup failed: {result.error_message}")
        sys.exit(1)

    console.print("\n[green]✨ Branch cleanup complete![/green]")
    console.print(GITHUB_FOOTER_LINE)


def display_cleanup_pr_file_result(file_result: FileCleanupPrResult) -> None:
    """Display cleanup results for a single file.

    Args:
        file_result: Per-file cleanup-PR result to display
    """
    status = STATUS_SUCCESS if file_result.success else STATUS_FAILURE
    console.print(f"  {status} {rich_escape(str(file_result.file_path))}")
    console.print(f"      issues: {file_result.issues_fixed} fixed, {file_result.issues_out_of_scope} out of scope")
    console.print(
        f"      duplications: {file_result.duplications_fixed} fixed, "
        f"{file_result.duplications_out_of_scope} out of scope"
    )
    if file_result.main_duplications_fixed or file_result.main_duplications_skipped:
        console.print(
            f"      main duplications: {file_result.main_duplications_fixed} fixed, "
            f"{file_result.main_duplications_skipped} skipped"
        )
    if file_result.error_message:
        error(f"      Error: {file_result.error_message}")


def display_cleanup_pr_results(result: CleanupPrResult, dry_run: bool = False) -> None:
    """Display PR-scoped cleanup results.

    Args:
        result: Cleanup-PR result to display
        dry_run: Whether this was a dry run (nothing was fixed or committed)
    """
    console.print("\n[bold]Cleanup Summary (PR scope):[/bold]")
    console.print(f"  Files processed: {len(result.files_processed)}")
    success(f"  Total issues fixed: {result.total_issues_fixed}")
    success(f"  Total duplications fixed: {result.total_duplications_fixed}")
    success(f"  Total main duplications fixed: {result.total_main_duplications_fixed}")
    if dry_run:
        dim("  Dry run: no changes were made")

    if result.external_files_touched:
        warn("\nFiles outside the branch diff were modified by duplication refactors:")
        for touched in result.external_files_touched:
            console.print(f"  {rich_escape(str(touched))}")

    if result.files_processed:
        console.print(PER_FILE_RESULTS_HEADER)
        for file_result in result.files_processed:
            display_cleanup_pr_file_result(file_result)

    if not result.success:
        if result.error_message:
            error(f"\nCleanup failed: {result.error_message}")
        sys.exit(1)

    console.print("\n[green]✨ Branch cleanup (PR scope) complete![/green]")
    console.print(GITHUB_FOOTER_LINE)


def display_dedupe_branch_results(result: DedupeBranchResult) -> None:
    """Display dedupe-branch results.

    Args:
        result: Dedupe-branch result to display
    """
    console.print("\n[bold]Deduplication Summary:[/bold]")
    console.print(f"  Files processed: {len(result.files_processed)}")
    success(f"  Total duplications fixed: {result.total_duplications_fixed}")

    if result.files_processed:
        console.print(PER_FILE_RESULTS_HEADER)
        for file_result in result.files_processed:
            status = STATUS_SUCCESS if file_result.success else STATUS_FAILURE
            console.print(
                f"  {status} {rich_escape(str(file_result.file_path))}: {file_result.duplications_fixed} duplications fixed"
            )
            if file_result.error_message:
                error(f"      Error: {file_result.error_message}")

    if not result.success:
        if result.error_message:
            error(f"\nDeduplication failed: {result.error_message}")
        sys.exit(1)

    console.print("\n[green]✨ Branch deduplication complete![/green]")
    console.print(GITHUB_FOOTER_LINE)


def display_stale_projects(stale_projects: list[StaleProject]) -> None:
    """Display a list of stale temp projects.

    Args:
        stale_projects: Stale projects found by ProjectManager.find_stale_projects
    """
    bold(f"\nFound {len(stale_projects)} stale project(s):")
    for project in stale_projects:
        age = project.age_minutes
        age_display = f"{age / 60:.1f}h" if age >= 60 else f"{age:.0f}m"
        console.print(f"  - {rich_escape(project.project_key)} (age: {age_display}, 0 analyses)")


SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]


def get_highest_severity(
    issues: list[ReviewIssue],
    severity_order: list[str] | None = None,
) -> str:
    """Return the highest severity string for a list of issues."""
    if not issues:
        return "N/A"
    order = SEVERITY_ORDER if severity_order is None else severity_order
    return min(
        (issue.severity for issue in issues),
        key=lambda s: order.index(s) if s in order else len(order),
    )


def build_review_table(result: ReviewAnalysisResult) -> None:
    """Build and print the per-file breakdown table for review results."""
    if not result.files:
        return

    console.print("\n[bold]Per-File Breakdown:[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("File", style="cyan")
    table.add_column("Issues", justify="right")
    table.add_column("Highest Severity", justify="right")
    table.add_column("Duplications", justify="right")
    show_coverage = any(f.coverage_pct is not None for f in result.files)
    if show_coverage:
        table.add_column("Coverage", justify="right")

    for file_review in result.files:
        issue_count = len(file_review.issues)
        dup_count = len(file_review.duplications) + len(file_review.resolved_duplications)
        highest_severity = get_highest_severity(file_review.issues)
        row: list[str] = [file_review.file_path, str(issue_count), highest_severity, str(dup_count)]
        if show_coverage:
            cov_str = f"{file_review.coverage_pct}%" if file_review.coverage_pct is not None else "—"
            row.append(cov_str)
        table.add_row(*row)

    console.print(table)


def display_review_results(result: ReviewAnalysisResult) -> None:
    """Display review analysis results.

    Args:
        result: ReviewAnalysisResult from the orchestrator.
    """
    total_issues = result.total_issues
    total_duplications = result.total_duplications
    files_checked = result.files_analyzed

    console.print("\n[bold]Review Summary:[/bold]")
    console.print(f"  Branch: {rich_escape(result.branch)} (base: {rich_escape(result.base_branch)})")
    console.print(f"  Files checked: {files_checked}")
    console.print(f"  Total issues: {total_issues}")
    console.print(f"  Duplication findings: {total_duplications}")

    if result.files:
        build_review_table(result)
    elif result.success and total_issues == 0 and total_duplications == 0:
        console.print("\n[green]No issues found on changed lines.[/green]")

    if result.report_file:
        dim(f"\nReport saved to {result.report_file}")


def display_baseline_results(result: BaselineScanResult) -> None:
    """Display baseline scan results.

    Args:
        result: BaselineScanResult from the orchestrator.
    """
    if result.success:
        success(f"Baseline scan complete: project {result.project_key} re-analyzed.")
        if result.dashboard_url:
            dim(f"Dashboard: {result.dashboard_url}")
    else:
        error(f"Baseline scan failed: {result.error_message}")
