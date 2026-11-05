"""Issue processor for sorting and filtering SonarQube issues."""

import logging

from dotharness.vibe_heal.processor.models import ProcessingResult
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue

logger = logging.getLogger(__name__)


class IssueProcessor:
    """Processes SonarQube issues to determine fix order.

    Filters issues by fixability and severity, sorts them in reverse line order
    (highest line number first) to avoid line number shifts, and optionally limits
    the number of issues to process.
    """

    def __init__(
        self,
        min_severity: str | None = None,
        max_issues: int | None = None,
    ) -> None:
        """Initialize the issue processor.

        Args:
            min_severity: Minimum severity to process (BLOCKER, CRITICAL, MAJOR, MINOR, INFO)
            max_issues: Maximum number of issues to process

        Raises:
            ValueError: If min_severity is not a recognised severity.
        """
        self.min_severity = min_severity
        self.max_issues = max_issues

        # Severity ranking (higher is more severe)
        self._severity_rank = {
            "BLOCKER": 5,
            "CRITICAL": 4,
            "MAJOR": 3,
            "MINOR": 2,
            "INFO": 1,
        }

        if min_severity and min_severity.upper() not in self._severity_rank:
            valid = ", ".join(sorted(self._severity_rank, key=self._severity_rank.__getitem__, reverse=True))
            raise ValueError(f"Invalid min_severity {min_severity!r}; expected one of: {valid}")  # noqa: TRY003

    def process(self, issues: list[SonarQubeIssue]) -> ProcessingResult:
        """Process issues: filter, sort, and limit."""
        total = len(issues)
        logger.debug(f"Processing {total} issues")

        fixable_issues = self._filter_fixable_issues(issues, total)
        severity_filtered_issues = self._filter_by_severity(fixable_issues)
        sorted_issues = self._sort_by_line_descending(severity_filtered_issues)
        final_issues = self._limit_issues(sorted_issues)

        return ProcessingResult(
            total_issues=total,
            fixable_issues=len(severity_filtered_issues),
            skipped_issues=total - len(final_issues),
            issues_to_fix=final_issues,
        )

    def _filter_fixable_issues(self, issues: list[SonarQubeIssue], total: int) -> list[SonarQubeIssue]:
        """Filter for issues that are fixable and log unfixable ones."""
        fixable = []
        for issue in issues:
            if issue.is_fixable:
                fixable.append(issue)
            else:
                self._log_unfixable_issue(issue)

        logger.debug(f"Step 1: {len(fixable)}/{total} issues are fixable")
        return fixable

    def _log_unfixable_issue(self, issue: SonarQubeIssue) -> None:
        """Log why an issue is not fixable."""
        message_preview = f"{issue.message[:50]}..." if issue.message and len(issue.message) > 50 else issue.message
        if issue.line is None:
            logger.debug(
                f"Issue {issue.key} not fixable: no line number (rule={issue.rule}, message={message_preview})"
            )
        else:
            logger.debug(
                f"Issue {issue.key} not fixable: status={issue.status!r} "
                f"(rule={issue.rule}, line={issue.line}, message={message_preview})"
            )

    def _filter_by_severity(self, issues: list[SonarQubeIssue]) -> list[SonarQubeIssue]:
        """Filter issues by minimum severity."""
        if not self.min_severity:
            return issues

        min_rank = self._severity_rank.get(self.min_severity.upper(), 0)
        before_count = len(issues)
        filtered = [issue for issue in issues if self._severity_rank.get(issue.severity or "INFO", 0) >= min_rank]
        logger.debug(
            f"Step 2: {len(filtered)}/{before_count} issues meet severity threshold (min_severity={self.min_severity})"
        )
        return filtered

    def _limit_issues(self, issues: list[SonarQubeIssue]) -> list[SonarQubeIssue]:
        """Limit the number of issues to process."""
        if not self.max_issues or self.max_issues <= 0:
            return issues

        before_count = len(issues)
        limited = issues[: self.max_issues]
        logger.debug(f"Step 4: limited to {len(limited)}/{before_count} issues (max_issues={self.max_issues})")
        return limited

    def _sort_by_line_descending(
        self,
        issues: list[SonarQubeIssue],
    ) -> list[SonarQubeIssue]:
        """Sort issues by line number in descending order.

        Issues without line numbers are placed at the end (though they should
        have been filtered out by is_fixable).

        Args:
            issues: List of issues to sort

        Returns:
            Sorted list of issues
        """
        # Separate issues with and without line numbers
        with_line = [issue for issue in issues if issue.line is not None]
        without_line = [issue for issue in issues if issue.line is None]

        # Sort issues with line numbers in descending order
        with_line.sort(key=lambda x: x.line or 0, reverse=True)

        # Return issues with line numbers first, then those without
        return with_line + without_line
