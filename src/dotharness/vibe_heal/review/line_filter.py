"""Filter SonarQube issues to only those on changed lines."""

import logging

from dotharness.vibe_heal.review.models import ReviewIssue
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue

logger = logging.getLogger(__name__)


class IssueLineFilter:
    """Filter SonarQube issues down to only those appearing on changed lines."""

    @staticmethod
    def filter_issues(
        issues: list[SonarQubeIssue],
        changed_lines: set[int],
        source_is_new_map: dict[int, bool] | None = None,
        strict_changed_lines: set[int] | None = None,
    ) -> list[ReviewIssue]:
        """Filter issues to only those on lines that were changed.

        Args:
            issues: SonarQube issues to filter.
            changed_lines: Set of line numbers that were changed in the branch
                (may include trailing context lines added by DiffParser).
            source_is_new_map: Optional mapping of line number to whether
                SonarQube considers the line new.
            strict_changed_lines: The unexpanded set of genuinely changed lines
                (no trailing context). When provided, issues that fall only in
                the trailing window are included but marked on_changed_line=False
                so the GitHub client knows not to attempt inline posting.

        Returns:
            List of ReviewIssue for issues on changed lines.
        """
        result: list[ReviewIssue] = []

        for issue in issues:
            if issue.line is None:
                continue

            if issue.line not in changed_lines:
                continue

            is_new = source_is_new_map.get(issue.line, False) if source_is_new_map else False

            if source_is_new_map is not None and issue.line in source_is_new_map and not is_new:
                logger.debug(
                    "Line %d is changed per git diff but SonarQube marks it as not new",
                    issue.line,
                )

            on_changed_line = strict_changed_lines is None or issue.line in strict_changed_lines

            result.append(
                ReviewIssue(
                    rule=issue.rule,
                    message=issue.message,
                    line=issue.line,
                    severity=issue.severity or "INFO",
                    doc_url=f"https://next.sonarqube.com/sonarqube/coding_rules?open={issue.rule}&rule_key={issue.rule}",
                    is_new_in_sonar=is_new,
                    on_changed_line=on_changed_line,
                )
            )

        return result
