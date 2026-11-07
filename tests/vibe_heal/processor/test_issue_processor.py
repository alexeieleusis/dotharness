"""Tests for IssueProcessor."""

import pytest

from dotharness.vibe_heal.processor import IssueProcessor
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


def create_issue(
    key: str,
    line: int | None,
    severity: str = "MAJOR",
    status: str = "OPEN",
) -> SonarQubeIssue:
    """Helper to create test issues."""
    return SonarQubeIssue(
        key=key,
        rule="test:rule",
        severity=severity,
        message=f"Issue {key}",
        component="test.py",
        line=line,
        status=status,
        type="CODE_SMELL",
    )


class TestIssueProcessor:
    """Tests for IssueProcessor class."""

    def test_sort_by_line_descending(self) -> None:
        """Test issues are sorted by line number in descending order."""
        issues = [
            create_issue("1", line=10),
            create_issue("2", line=50),
            create_issue("3", line=30),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        # Should be sorted: 50, 30, 10
        assert result.issues_to_fix[0].line == 50
        assert result.issues_to_fix[1].line == 30
        assert result.issues_to_fix[2].line == 10

    def test_handle_issues_without_line_numbers(self) -> None:
        """Test issues without line numbers are filtered out."""
        issues = [
            create_issue("1", line=10),
            create_issue("2", line=None),  # No line number - not fixable
            create_issue("3", line=30),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        # Issue without line number should be filtered out
        assert len(result.issues_to_fix) == 2
        assert result.issues_to_fix[0].line == 30
        assert result.issues_to_fix[1].line == 10

    def test_handle_empty_list(self) -> None:
        """Test processing empty list of issues."""
        processor = IssueProcessor()
        result = processor.process([])

        assert result.total_issues == 0
        assert result.fixable_issues == 0
        assert result.skipped_issues == 0
        assert len(result.issues_to_fix) == 0
        assert not result.has_issues

    def test_handle_single_issue(self) -> None:
        """Test processing single issue."""
        issues = [create_issue("1", line=42)]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert result.total_issues == 1
        assert result.fixable_issues == 1
        assert result.skipped_issues == 0
        assert len(result.issues_to_fix) == 1
        assert result.issues_to_fix[0].line == 42
        assert result.has_issues

    def test_handle_all_issues_same_line(self) -> None:
        """Test all issues on the same line."""
        issues = [
            create_issue("1", line=10),
            create_issue("2", line=10),
            create_issue("3", line=10),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert len(result.issues_to_fix) == 3
        # All should be on line 10
        assert all(issue.line == 10 for issue in result.issues_to_fix)

    def test_filter_non_fixable_no_line(self) -> None:
        """Test filtering issues without line numbers."""
        issues = [
            create_issue("1", line=None),
            create_issue("2", line=None),
            create_issue("3", line=10),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert result.total_issues == 3
        assert result.fixable_issues == 1
        assert len(result.issues_to_fix) == 1
        assert result.issues_to_fix[0].key == "3"

    def test_filter_resolved_issues(self) -> None:
        """Test filtering resolved/closed issues."""
        issues = [
            create_issue("1", line=10, status="OPEN"),
            create_issue("2", line=20, status="RESOLVED"),
            create_issue("3", line=30, status="CLOSED"),
            create_issue("4", line=40, status="WONTFIX"),
            create_issue("5", line=50, status="ACCEPTED"),
            create_issue("6", line=60, status="OPEN"),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        # Only OPEN issues should be processed
        assert len(result.issues_to_fix) == 2
        assert result.issues_to_fix[0].line == 60
        assert result.issues_to_fix[1].line == 10

    def test_filter_by_severity_blocker_only(self) -> None:
        """Test filtering by BLOCKER severity."""
        issues = [
            create_issue("1", line=10, severity="BLOCKER"),
            create_issue("2", line=20, severity="CRITICAL"),
            create_issue("3", line=30, severity="MAJOR"),
            create_issue("4", line=40, severity="MINOR"),
            create_issue("5", line=50, severity="INFO"),
        ]

        processor = IssueProcessor(min_severity="BLOCKER")
        result = processor.process(issues)

        # Only BLOCKER should be included
        assert len(result.issues_to_fix) == 1
        assert result.issues_to_fix[0].severity == "BLOCKER"

    def test_filter_by_severity_major_and_above(self) -> None:
        """Test filtering by MAJOR severity and above."""
        issues = [
            create_issue("1", line=10, severity="BLOCKER"),
            create_issue("2", line=20, severity="CRITICAL"),
            create_issue("3", line=30, severity="MAJOR"),
            create_issue("4", line=40, severity="MINOR"),
            create_issue("5", line=50, severity="INFO"),
        ]

        processor = IssueProcessor(min_severity="MAJOR")
        result = processor.process(issues)

        # BLOCKER, CRITICAL, MAJOR should be included (sorted by line descending)
        assert len(result.issues_to_fix) == 3
        # Sorted by line number: 30, 20, 10
        assert result.issues_to_fix[0].line == 30
        assert result.issues_to_fix[0].severity == "MAJOR"
        assert result.issues_to_fix[1].line == 20
        assert result.issues_to_fix[1].severity == "CRITICAL"
        assert result.issues_to_fix[2].line == 10
        assert result.issues_to_fix[2].severity == "BLOCKER"

    def test_all_severities_when_no_min_specified(self) -> None:
        """Test all severities are processed when no minimum specified."""
        issues = [
            create_issue("1", line=10, severity="BLOCKER"),
            create_issue("2", line=20, severity="INFO"),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert len(result.issues_to_fix) == 2

    def test_limit_to_n_issues(self) -> None:
        """Test limiting number of issues to process."""
        issues = [create_issue(str(i), line=i * 10) for i in range(1, 11)]

        processor = IssueProcessor(max_issues=5)
        result = processor.process(issues)

        assert len(result.issues_to_fix) == 5
        # Should get top 5 (highest line numbers after sorting)
        assert result.issues_to_fix[0].line == 100
        assert result.issues_to_fix[4].line == 60

    def test_limit_greater_than_available(self) -> None:
        """Test limit greater than number of available issues."""
        issues = [create_issue(str(i), line=i * 10) for i in range(1, 4)]

        processor = IssueProcessor(max_issues=10)
        result = processor.process(issues)

        # Should get all 3 issues
        assert len(result.issues_to_fix) == 3

    def test_no_limit_specified(self) -> None:
        """Test processing all issues when no limit specified."""
        issues = [create_issue(str(i), line=i * 10) for i in range(1, 11)]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert len(result.issues_to_fix) == 10

    def test_complex_scenario(self) -> None:
        """Test complex scenario with multiple filters, sorting, and limiting."""
        issues = [
            create_issue("1", line=10, severity="BLOCKER", status="OPEN"),
            create_issue("2", line=20, severity="MAJOR", status="RESOLVED"),
            create_issue("3", line=30, severity="CRITICAL", status="OPEN"),
            create_issue("4", line=40, severity="MINOR", status="OPEN"),
            create_issue("5", line=50, severity="MAJOR", status="OPEN"),
            create_issue("6", line=None, severity="BLOCKER", status="OPEN"),
            create_issue("7", line=70, severity="INFO", status="OPEN"),
            create_issue("8", line=80, severity="MAJOR", status="OPEN"),
        ]

        processor = IssueProcessor(min_severity="MAJOR", max_issues=3)
        result = processor.process(issues)

        # Should filter: MAJOR and above, OPEN status, with line numbers
        # Then sort descending and limit to 3
        assert len(result.issues_to_fix) == 3
        assert result.issues_to_fix[0].line == 80  # MAJOR
        assert result.issues_to_fix[1].line == 50  # MAJOR
        assert result.issues_to_fix[2].line == 30  # CRITICAL
        assert result.total_issues == 8
        # fixable_issues = after is_fixable AND severity filter (excludes None, RESOLVED, MINOR, INFO)
        assert result.fixable_issues == 4

    def test_processing_result_properties(self) -> None:
        """Test ProcessingResult properties."""
        issues = [
            create_issue("1", line=10),
            create_issue("2", line=None),
            create_issue("3", line=30, status="RESOLVED"),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert result.total_issues == 3
        assert result.fixable_issues == 1
        assert result.skipped_issues == 2
        assert result.has_issues is True

    def test_has_issues_property_false(self) -> None:
        """Test has_issues property when no issues to fix."""
        issues = [
            create_issue("1", line=None),
            create_issue("2", line=20, status="RESOLVED"),
        ]

        processor = IssueProcessor()
        result = processor.process(issues)

        assert result.has_issues is False

    def test_case_insensitive_severity(self) -> None:
        """Test severity filtering is case insensitive."""
        issues = [
            create_issue("1", line=10, severity="MAJOR"),
            create_issue("2", line=20, severity="MINOR"),
        ]

        processor = IssueProcessor(min_severity="major")
        result = processor.process(issues)

        assert len(result.issues_to_fix) == 1
        assert result.issues_to_fix[0].severity == "MAJOR"

    def test_unknown_severity_handled(self) -> None:
        """Test unknown severity is handled gracefully."""
        issues = [
            create_issue("1", line=10, severity="UNKNOWN"),
            create_issue("2", line=20, severity="MAJOR"),
        ]

        processor = IssueProcessor(min_severity="MAJOR")
        result = processor.process(issues)

        # UNKNOWN severity (rank 0) should be filtered out when min is MAJOR
        assert len(result.issues_to_fix) == 1
        assert result.issues_to_fix[0].severity == "MAJOR"


def test_invalid_min_severity_raises() -> None:
    """An unrecognised min_severity must not silently disable the filter."""
    with pytest.raises(ValueError, match="MAJR"):
        IssueProcessor(min_severity="MAJR")
