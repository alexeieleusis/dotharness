"""Tests for cleanup_pr.scope (FR-4 issue selection, FR-5 duplication selection)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dotharness.vibe_heal.cleanup_pr.scope import select_in_scope_duplications, select_in_scope_issues
from dotharness.vibe_heal.deduplication.models import DuplicationBlock, DuplicationGroup, DuplicationsResponse
from dotharness.vibe_heal.review.models import ReviewIssue
from dotharness.vibe_heal.review.orchestrator import ReviewOrchestrator
from dotharness.vibe_heal.sonarqube.models import SonarQubeIssue


def _make_issue(
    key: str,
    line: int | None = 10,
    rule: str = "python:S1481",
    status: str | None = None,
) -> SonarQubeIssue:
    """Build a SonarQubeIssue with sensible defaults."""
    return SonarQubeIssue(
        key=key,
        rule=rule,
        message=f"Issue {key}",
        component="src/main.py",
        line=line,
        status=status,
    )


def _make_group(from_line: int, size: int, target_ref: str = "1") -> DuplicationGroup:
    """Build a DuplicationGroup with a target block plus one other-file block."""
    target_block = DuplicationBlock(**{"from": from_line, "size": size, "_ref": target_ref})
    other_block = DuplicationBlock(**{"from": 50, "size": 10, "_ref": "2"})
    return DuplicationGroup(blocks=[target_block, other_block])


# Strict changed lines {10, 14} with a 3-line trailing window produce the
# new-line set {10, 11, 12, 13, 14, 15, 16, 17}.
STRICT_LINES = {10, 14}
WINDOW_LINES = {10, 11, 12, 13, 14, 15, 16, 17}


class TestSelectInScopeIssues:
    """Tests for select_in_scope_issues()."""

    def test_keeps_issue_on_changed_line(self) -> None:
        """A fixable issue on a strict changed line is kept."""
        issues = [_make_issue("a", line=10)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == issues
        assert result.out_of_scope_count == 0

    def test_keeps_issue_in_trailing_window(self) -> None:
        """A window-only issue (not on a strict line) is still in scope."""
        issues = [_make_issue("a", line=17)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == issues
        assert result.out_of_scope_count == 0

    def test_drops_issue_outside_scope(self) -> None:
        """An issue outside the trailing window is dropped and counted out of scope."""
        issues = [_make_issue("a", line=5), _make_issue("b", line=10)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == [issues[1]]
        assert result.out_of_scope_count == 1

    def test_keeps_issue_on_first_strict_line(self) -> None:
        """An issue on the first strict changed line is kept."""
        issues = [_make_issue("a", line=10)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == issues

    def test_keeps_issue_on_last_strict_line(self) -> None:
        """An issue on the last strict changed line is kept."""
        issues = [_make_issue("a", line=14)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == issues

    def test_keeps_issue_on_last_window_line(self) -> None:
        """An issue on the last trailing-window line is kept."""
        issues = [_make_issue("a", line=17)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == issues

    def test_drops_issue_on_first_line_beyond_window(self) -> None:
        """An issue on the first line beyond the trailing window is dropped."""
        issues = [_make_issue("a", line=18)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 1

    def test_drops_issue_without_line(self) -> None:
        """An issue with line=None is dropped and not counted (not fixable)."""
        issues = [_make_issue("a", line=None)]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 0

    def test_drops_non_fixable_issue_on_in_scope_line(self) -> None:
        """A resolved issue on an in-scope line is dropped and not counted out of scope."""
        issues = [_make_issue("a", line=10, status="RESOLVED")]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 0

    def test_drops_non_fixable_issue_outside_scope(self) -> None:
        """A resolved issue outside the window is dropped and not counted out of scope."""
        issues = [_make_issue("a", line=5, status="RESOLVED")]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 0

    def test_out_of_scope_counts_fixable_but_dropped(self) -> None:
        """Out of scope = fixable issues minus kept issues (non-fixable excluded)."""
        issues = [
            _make_issue("a", line=10),  # fixable, in scope -> kept
            _make_issue("b", line=5),  # fixable, out of scope -> counted
            _make_issue("c", line=14, status="RESOLVED"),  # not fixable -> not counted
            _make_issue("d", line=None),  # not fixable -> not counted
        ]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == [issues[0]]
        assert result.out_of_scope_count == 1

    def test_same_rule_same_line_selects_all(self) -> None:
        """A surviving (rule, line) pair matching several records keeps all of them."""
        issues = [
            _make_issue("a", line=10, rule="python:S1481"),
            _make_issue("b", line=10, rule="python:S1481"),
            _make_issue("c", line=10, rule="python:S1481"),
        ]

        result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == issues
        assert result.out_of_scope_count == 0

    def test_empty_issues(self) -> None:
        """An empty issue list yields an empty result."""
        result = select_in_scope_issues([], WINDOW_LINES, STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 0

    def test_passes_resolved_line_sets_to_filter_unchanged(self) -> None:
        """The resolved line sets are handed to IssueLineFilter without modification."""
        issues = [_make_issue("a", line=10), _make_issue("b", line=18)]
        surviving = [ReviewIssue(rule="python:S1481", message="Issue a", line=10)]

        with patch("dotharness.vibe_heal.cleanup_pr.scope.IssueLineFilter") as mock_filter:
            mock_filter.filter_issues.return_value = surviving
            result = select_in_scope_issues(issues, WINDOW_LINES, STRICT_LINES)

        mock_filter.filter_issues.assert_called_once_with(
            issues,
            WINDOW_LINES,
            strict_changed_lines=STRICT_LINES,
        )
        assert result.in_scope == [issues[0]]
        assert result.out_of_scope_count == 1


class TestSelectInScopeDuplications:
    """Tests for select_in_scope_duplications()."""

    def test_keeps_group_intersecting_strict_lines(self) -> None:
        """A group whose target block covers a strict changed line is kept."""
        groups = [_make_group(10, 15)]  # block lines 10-24; strict lines 10 and 14 inside

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == groups
        assert result.out_of_scope_count == 0

    def test_drops_group_not_intersecting_strict_lines(self) -> None:
        """A group whose target block misses all strict lines is dropped and counted out of scope."""
        groups = [_make_group(100, 10)]  # block lines 100-109

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 1

    def test_drops_group_touching_only_window_not_strict(self) -> None:
        """A block touching only the 3-line trailing window, not the strict lines, is excluded."""
        groups = [_make_group(15, 3)]  # block lines 15-17: inside WINDOW_LINES, outside STRICT_LINES

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 1

    def test_keeps_group_touching_from_line_edge(self) -> None:
        """A block whose from_line equals a strict changed line is kept."""
        groups = [_make_group(14, 5)]  # block lines 14-18; from_line == 14 is strict

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == groups
        assert result.out_of_scope_count == 0

    def test_keeps_group_touching_to_line_edge(self) -> None:
        """A block whose to_line equals a strict changed line is kept."""
        groups = [_make_group(5, 6)]  # block lines 5-10; to_line == 10 is strict

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == groups
        assert result.out_of_scope_count == 0

    def test_drops_group_without_target_block(self) -> None:
        """A group with no block for the target ref is dropped and counted out of scope."""
        groups = [_make_group(10, 15, target_ref="2")]  # no block for target ref "1"

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 1

    def test_out_of_scope_counts_considered_minus_kept(self) -> None:
        """Out of scope = groups considered minus groups kept."""
        groups = [
            _make_group(10, 15),  # kept: target block intersects strict lines
            _make_group(100, 10),  # dropped: target block misses strict lines
            _make_group(15, 3),  # dropped: target block touches only the window
        ]

        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        assert result.in_scope == [groups[0]]
        assert result.out_of_scope_count == 2

    def test_empty_groups(self) -> None:
        """An empty group list yields an empty result."""
        result = select_in_scope_duplications([], "1", STRICT_LINES)

        assert result.in_scope == []
        assert result.out_of_scope_count == 0


class TestActiveFindingBehaviorUnchanged:
    """Extracting changed_lines_in_block leaves _build_active_finding behaving as before."""

    @pytest.fixture
    def orchestrator(self) -> ReviewOrchestrator:
        from dotharness.vibe_heal.config import VibeHealConfig

        config = VibeHealConfig(
            sonarqube_url="https://sonar.test.com",
            sonarqube_token="test-token",
            sonarqube_project_key="temp-project",
        )
        mock_client = AsyncMock()
        mock_analyzer = MagicMock()
        mock_analyzer.repo.working_dir = "/repo"
        mock_parser = MagicMock()
        return ReviewOrchestrator(config, mock_client, mock_analyzer, mock_parser)

    @staticmethod
    def _make_response() -> MagicMock:
        """Build a mock DuplicationsResponse whose file-info lookup succeeds."""
        response = MagicMock(spec=DuplicationsResponse)
        file_info = MagicMock()
        file_info.key = "temp-project:src/file.py"
        response.get_file_info.return_value = file_info
        return response

    def test_finding_built_when_block_intersects_strict_lines(self, orchestrator) -> None:
        """A block covering strict lines still yields a ReviewDuplication with the lowest strict line as anchor."""
        group = _make_group(10, 15)  # block lines 10-24; strict lines {10, 14} inside

        finding = orchestrator._build_active_finding(group, "1", STRICT_LINES, self._make_response())

        assert finding is not None
        assert finding.from_line == 10
        assert finding.to_line == 24
        assert finding.anchor_line == 10

    def test_no_finding_when_block_misses_strict_lines(self, orchestrator) -> None:
        group = _make_group(100, 10)  # block lines 100-109

        finding = orchestrator._build_active_finding(group, "1", STRICT_LINES, self._make_response())

        assert finding is None

    def test_no_finding_when_block_touches_only_window(self, orchestrator) -> None:
        group = _make_group(15, 3)  # block lines 15-17: window only, not strict

        finding = orchestrator._build_active_finding(group, "1", STRICT_LINES, self._make_response())

        assert finding is None

    def test_finding_decision_matches_scope_predicate(self, orchestrator) -> None:
        """_build_active_finding keeps a group iff select_in_scope_duplications does."""
        groups = [_make_group(10, 15), _make_group(100, 10), _make_group(15, 3)]
        result = select_in_scope_duplications(groups, "1", STRICT_LINES)

        finding_decisions = [
            orchestrator._build_active_finding(group, "1", STRICT_LINES, self._make_response()) is not None
            for group in groups
        ]

        assert result.in_scope == [groups[0]]
        assert finding_decisions == [True, False, False]
