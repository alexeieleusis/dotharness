"""Tests for SonarQube models."""

from typing import Any

import pytest

from dotharness.vibe_heal.sonarqube.models import IssuesResponse, SonarQubeIssue, SourceLine


class TestSonarQubeIssue:
    """Tests for SonarQubeIssue model."""

    def test_create_issue_with_line(self) -> None:
        """Test creating an issue with a line number."""
        issue = SonarQubeIssue(
            key="issue-123",
            rule="python:S1481",
            severity="MAJOR",
            message="Remove unused import",
            component="project:src/main.py",
            line=10,
            status="OPEN",
            type="CODE_SMELL",
        )

        assert issue.key == "issue-123"
        assert issue.rule == "python:S1481"
        assert issue.severity == "MAJOR"
        assert issue.message == "Remove unused import"
        assert issue.component == "project:src/main.py"
        assert issue.line == 10
        assert issue.status == "OPEN"
        assert issue.type == "CODE_SMELL"

    def test_create_issue_without_line(self) -> None:
        """Test creating an issue without a line number."""
        issue = SonarQubeIssue(
            key="issue-456",
            rule="python:S999",
            severity="INFO",
            message="File-level issue",
            component="project:src/main.py",
            status="OPEN",
            type="CODE_SMELL",
        )

        assert issue.line is None

    def test_is_fixable_with_line_and_open_status(self) -> None:
        """Test is_fixable returns True for open issue with line number."""
        issue = SonarQubeIssue(
            key="issue-1",
            rule="python:S1481",
            severity="MAJOR",
            message="Issue",
            component="file.py",
            line=10,
            status="OPEN",
            type="CODE_SMELL",
        )

        assert issue.is_fixable is True

    def test_is_fixable_without_line(self) -> None:
        """Test is_fixable returns False for issue without line number."""
        issue = SonarQubeIssue(
            key="issue-2",
            rule="python:S999",
            severity="INFO",
            message="Issue",
            component="file.py",
            status="OPEN",
            type="CODE_SMELL",
        )

        assert issue.is_fixable is False

    @pytest.mark.parametrize(
        "status",
        ["RESOLVED", "CLOSED", "WONTFIX", "FALSE-POSITIVE"],
    )
    def test_is_fixable_with_resolved_status(self, status: str) -> None:
        """Test is_fixable returns False for resolved issues."""
        issue = SonarQubeIssue(
            key="issue-3",
            rule="python:S1481",
            severity="MAJOR",
            message="Issue",
            component="file.py",
            line=10,
            status=status,
            type="CODE_SMELL",
        )

        assert issue.is_fixable is False

    def test_is_fixable_with_confirmed_status(self) -> None:
        """Test is_fixable returns True for confirmed issue."""
        issue = SonarQubeIssue(
            key="issue-4",
            rule="python:S1481",
            severity="MAJOR",
            message="Issue",
            component="file.py",
            line=10,
            status="CONFIRMED",
            type="CODE_SMELL",
        )

        assert issue.is_fixable is True


class TestIssuesResponse:
    """Tests for IssuesResponse model."""

    def test_parse_response_with_issues(self) -> None:
        """Test parsing a response with issues."""
        data = {
            "total": 2,
            "p": 1,
            "ps": 100,
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 2},
            "issues": [
                {
                    "key": "issue-1",
                    "rule": "python:S1481",
                    "severity": "MAJOR",
                    "message": "Issue 1",
                    "component": "project:file.py",
                    "line": 10,
                    "status": "OPEN",
                    "type": "CODE_SMELL",
                },
                {
                    "key": "issue-2",
                    "rule": "python:S1192",
                    "severity": "MINOR",
                    "message": "Issue 2",
                    "component": "project:file.py",
                    "line": 20,
                    "status": "OPEN",
                    "type": "CODE_SMELL",
                },
            ],
        }

        response = IssuesResponse(**data)

        assert response.total == 2
        assert response.p == 1
        assert response.ps == 100
        assert len(response.issues) == 2
        assert response.issues[0].key == "issue-1"
        assert response.issues[1].key == "issue-2"

    def test_parse_empty_response(self) -> None:
        """Test parsing an empty response."""
        data = {
            "total": 0,
            "p": 1,
            "ps": 100,
            "paging": {"pageIndex": 1, "pageSize": 100, "total": 0},
            "issues": [],
        }

        response = IssuesResponse(**data)

        assert response.total == 0
        assert len(response.issues) == 0

    def test_default_values(self) -> None:
        """Test default values for optional fields."""
        data: dict[str, Any] = {"total": 0, "p": 1, "ps": 100}

        response = IssuesResponse(**data)

        assert response.issues == []
        assert response.paging == {}


class TestSourceLineLineCoverage:
    def test_line_hits_parsed_when_present(self) -> None:
        sl = SourceLine.model_validate({"line": 24, "code": "x", "lineHits": 3})
        assert sl.line_hits == 3

    def test_line_hits_zero_parsed(self) -> None:
        sl = SourceLine.model_validate({"line": 24, "code": "x", "lineHits": 0})
        assert sl.line_hits == 0

    def test_line_hits_none_when_absent(self) -> None:
        sl = SourceLine.model_validate({"line": 24, "code": "x"})
        assert sl.line_hits is None

    def test_unknown_fields_still_ignored(self) -> None:
        sl = SourceLine.model_validate({
            "line": 1,
            "code": "y",
            "lineHits": 1,
            "utLineHits": 1,
            "someFutureField": "ignored",
        })
        assert sl.line_hits == 1
