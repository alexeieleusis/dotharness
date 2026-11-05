"""Tests for IssueLineFilter (changed-line filtering)."""

from typing import Any
from unittest.mock import patch

from dotharness.vibe_heal.review.line_filter import IssueLineFilter
from dotharness.vibe_heal.review.models import ReviewIssue
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


class TestIssueLineFilter:
    """Tests for IssueLineFilter.filter_issues()."""

    def _make_issue(self, line: int, **kwargs: Any) -> SonarQubeIssue:
        return SonarQubeIssue(
            key=f"issue-{line}",
            rule="python:S1481",
            message=f"Issue at line {line}",
            component="src/main.py",
            line=line,
            **kwargs,
        )

    def test_keeps_issues_on_changed_lines(self) -> None:
        """Issues whose line is in changed_lines are kept."""
        issues = [
            self._make_issue(10),
            self._make_issue(20),
            self._make_issue(30),
        ]
        changed_lines = {10, 30}

        result = IssueLineFilter.filter_issues(issues, changed_lines)

        assert len(result) == 2
        assert result[0].line == 10
        assert result[1].line == 30

    def test_discards_issues_on_unchanged_lines(self) -> None:
        """Issues whose line is NOT in changed_lines are dropped."""
        issues = [
            self._make_issue(10),
            self._make_issue(20),
            self._make_issue(30),
        ]
        changed_lines = {20}

        result = IssueLineFilter.filter_issues(issues, changed_lines)

        assert len(result) == 1
        assert result[0].line == 20

    def test_empty_changed_lines_returns_empty(self) -> None:
        """When changed_lines is empty, no issues are returned."""
        issues = [
            self._make_issue(10),
            self._make_issue(20),
        ]
        changed_lines: set[int] = set()

        result = IssueLineFilter.filter_issues(issues, changed_lines)

        assert result == []

    def test_empty_issues_returns_empty(self) -> None:
        """When issues list is empty, result is empty."""
        changed_lines = {10, 20, 30}

        result = IssueLineFilter.filter_issues([], changed_lines)

        assert result == []

    def test_multiple_issues_same_line(self) -> None:
        """Multiple issues on the same line are all kept if that line is changed."""
        issues = [
            SonarQubeIssue(
                key="issue-a",
                rule="python:S1481",
                message="Unused import",
                component="src/main.py",
                line=10,
            ),
            SonarQubeIssue(
                key="issue-b",
                rule="python:S1192",
                message="Unused variable",
                component="src/main.py",
                line=10,
            ),
        ]
        changed_lines = {10}

        result = IssueLineFilter.filter_issues(issues, changed_lines)

        assert len(result) == 2
        assert result[0].rule == "python:S1481"
        assert result[1].rule == "python:S1192"

    def test_is_new_in_sonar_populated_from_map(self) -> None:
        """is_new_in_sonar is populated from source_is_new_map when provided."""
        issues = [self._make_issue(10), self._make_issue(20)]
        changed_lines = {10, 20}
        source_is_new_map = {10: True, 20: False}

        result = IssueLineFilter.filter_issues(issues, changed_lines, source_is_new_map)

        assert result[0].is_new_in_sonar is True
        assert result[1].is_new_in_sonar is False

    def test_is_new_in_sonar_defaults_false_without_map(self) -> None:
        """is_new_in_sonar defaults to False when source_is_new_map is None."""
        issues = [self._make_issue(10)]
        changed_lines = {10}

        result = IssueLineFilter.filter_issues(issues, changed_lines, None)

        assert result[0].is_new_in_sonar is False

    def test_is_new_in_sonar_defaults_false_when_line_not_in_map(self) -> None:
        """is_new_in_sonar defaults to False when line is missing from the map."""
        issues = [self._make_issue(10)]
        changed_lines = {10}
        source_is_new_map = {20: True}

        result = IssueLineFilter.filter_issues(issues, changed_lines, source_is_new_map)

        assert result[0].is_new_in_sonar is False

    def test_logs_debug_discrepancy_when_git_changed_but_sonar_not_new(self) -> None:
        """DEBUG log is emitted when line is in changed_lines but SonarQube says not new."""
        issues = [self._make_issue(10)]
        changed_lines = {10}
        source_is_new_map = {10: False}

        with patch("dotharness.vibe_heal.review.line_filter.logger") as mock_logger:
            IssueLineFilter.filter_issues(issues, changed_lines, source_is_new_map)

            mock_logger.debug.assert_called_once()

    def test_no_debug_log_when_sonar_new_matches_changed(self) -> None:
        """No DEBUG discrepancy log when SonarQube isNew=True matches a changed line."""
        issues = [self._make_issue(10)]
        changed_lines = {10}
        source_is_new_map = {10: True}

        with patch("dotharness.vibe_heal.review.line_filter.logger") as mock_logger:
            IssueLineFilter.filter_issues(issues, changed_lines, source_is_new_map)

            mock_logger.debug.assert_not_called()

    def test_no_debug_log_without_source_map(self) -> None:
        """No DEBUG discrepancy log when source_is_new_map is not provided."""
        issues = [self._make_issue(10)]
        changed_lines = {10}

        with patch("dotharness.vibe_heal.review.line_filter.logger") as mock_logger:
            IssueLineFilter.filter_issues(issues, changed_lines, None)

            mock_logger.debug.assert_not_called()

    def test_converts_sonar_issue_to_review_issue(self) -> None:
        """Output ReviewIssue contains correct fields converted from SonarQubeIssue."""
        issue = SonarQubeIssue(
            key="issue-42",
            rule="python:S1481",
            message="Remove this unused import",
            component="src/main.py",
            line=42,
            severity="MAJOR",
        )
        changed_lines = {42}

        result = IssueLineFilter.filter_issues([issue], changed_lines)

        assert len(result) == 1
        assert isinstance(result[0], ReviewIssue)
        assert result[0].rule == "python:S1481"
        assert result[0].message == "Remove this unused import"
        assert result[0].line == 42
        assert result[0].severity == "MAJOR"
        assert result[0].doc_url == (
            "https://next.sonarqube.com/sonarqube/coding_rules?open=python:S1481&rule_key=python:S1481"
        )

    def test_strict_changed_lines_marks_trailing_context(self) -> None:
        """Issues in the trailing window are returned with on_changed_line=False."""
        issues = [self._make_issue(10), self._make_issue(11)]

        result = IssueLineFilter.filter_issues(issues, {10, 11}, strict_changed_lines={10})

        assert len(result) == 2
        assert result[0].line == 10
        assert result[0].on_changed_line is True
        assert result[1].line == 11
        assert result[1].on_changed_line is False

    def test_strict_changed_lines_none_marks_all_on_changed_line(self) -> None:
        """strict_changed_lines=None (default) gives on_changed_line=True for all results."""
        issues = [self._make_issue(10), self._make_issue(11)]

        result = IssueLineFilter.filter_issues(issues, {10, 11}, strict_changed_lines=None)

        assert len(result) == 2
        assert all(r.on_changed_line is True for r in result)

    def test_empty_strict_changed_lines_differs_from_none(self) -> None:
        """An empty strict set marks every issue on_changed_line=False, unlike None."""
        issues = [self._make_issue(10)]

        result = IssueLineFilter.filter_issues(issues, {10}, strict_changed_lines=set())

        assert len(result) == 1
        assert result[0].on_changed_line is False

    def test_strict_line_not_in_changed_lines_is_dropped(self) -> None:
        """An issue in strict_changed_lines but not in changed_lines is dropped."""
        issues = [self._make_issue(10), self._make_issue(20)]

        result = IssueLineFilter.filter_issues(issues, {10}, strict_changed_lines={10, 20})

        assert len(result) == 1
        assert result[0].line == 10

    def test_severity_none_falls_back_to_info(self) -> None:
        """A missing severity falls back to 'INFO'."""
        issues = [self._make_issue(10, severity=None)]

        result = IssueLineFilter.filter_issues(issues, {10})

        assert result[0].severity == "INFO"

    def test_skips_issues_without_line_number(self) -> None:
        """Issues without a line number are skipped (cannot match to changed lines)."""
        issues = [
            SonarQubeIssue(
                key="issue-noline",
                rule="python:S1481",
                message="File-level issue",
                component="src/main.py",
                line=None,
            ),
            self._make_issue(10),
        ]
        changed_lines = {10}

        result = IssueLineFilter.filter_issues(issues, changed_lines)

        assert len(result) == 1
        assert result[0].line == 10
