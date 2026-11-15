"""Tests for the review text that vibe-heal renders for a code host."""

from types import SimpleNamespace

import pytest

from dotharness.code_host.declarations import Anchor
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult
from dotharness.vibe_heal.review.render import MAX_OVERFLOW_BODY_LINES, build_fallback_body, build_review

_MAX_INLINE_COMMENTS = 50
_MAX_OVERFLOW_BODY_LINES = MAX_OVERFLOW_BODY_LINES


def _preview(report: ReviewResult) -> SimpleNamespace:
    body, inline_comments = build_review(report, _MAX_INLINE_COMMENTS)
    return SimpleNamespace(body=body, inline_comments=inline_comments)


@pytest.fixture
def sample_report() -> ReviewResult:
    return ReviewResult(
        project_key="test-project",
        branch="feature/test",
        base_branch="origin/main",
        files=[
            FileReview(
                file_path="src/example.py",
                issues=[
                    ReviewIssue(
                        rule="python:S1481",
                        message="Remove unused variable",
                        line=10,
                        severity="MAJOR",
                        doc_url="https://rules.example.com/S1481",
                    ),
                    ReviewIssue(rule="python:S1192", message="Useless import", line=25, severity="MINOR"),
                ],
            ),
        ],
    )


class TestBuildReview:
    @staticmethod
    def _n_issues(n: int) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path=f"src/file_{i}.py",
                    issues=[ReviewIssue(rule="python:S1", message=f"Issue {i}", line=i + 1, on_changed_line=True)],
                )
                for i in range(n)
            ],
        )

    def test_inline_comments_carry_right_side_and_line(self, sample_report) -> None:
        preview = _preview(sample_report)
        first = preview.inline_comments[0]
        assert first.anchor == Anchor("src/example.py", 10, "RIGHT")
        assert first.body.startswith("**python:S1481**")

    def test_nearby_issues_section(self) -> None:
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            files=[
                FileReview(
                    file_path="a.py",
                    issues=[ReviewIssue(rule="python:S1", message="near", line=7, on_changed_line=False)],
                )
            ],
        )
        preview = _preview(report)
        assert "**Issues near changed lines**" in preview.body
        assert "- **python:S1** (a.py:7) near" in preview.body
        assert "1 near changed lines" in preview.body
        assert preview.inline_comments == []

    def test_duplication_comments_use_anchor_line(self) -> None:
        from dotharness.vibe_heal.review.models import DuplicationLocation, ResolvedDuplication, ReviewDuplication

        loc = DuplicationLocation(file_path="other.py", from_line=1, to_line=5)
        report = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path="a.py",
                    duplications=[
                        ReviewDuplication(from_line=3, to_line=9, other_locations=[loc], anchor_line=4),
                        ReviewDuplication(from_line=20, to_line=29, other_locations=[loc]),
                    ],
                    resolved_duplications=[
                        ResolvedDuplication(main_from_line=1, main_to_line=4, anchor_new_line=2, other_locations=[loc])
                    ],
                )
            ],
        )
        preview = _preview(report)
        assert [c.anchor.line for c in preview.inline_comments] == [4, 20, 2]
        assert "**Duplication detected** (lines 3-9)" in preview.inline_comments[0].body
        assert "`other.py` lines 1-5" in preview.inline_comments[0].body
        assert "`origin/main`" in preview.inline_comments[2].body

    def test_comments_capped_at_max(self) -> None:
        preview = _preview(self._n_issues(_MAX_INLINE_COMMENTS + 10))
        assert len(preview.inline_comments) == _MAX_INLINE_COMMENTS

    def test_overflow_appears_in_body(self) -> None:
        preview = _preview(self._n_issues(_MAX_INLINE_COMMENTS + 5))
        assert "5 finding(s) over the" in preview.body
        assert "capped" in preview.body
        assert "src/file_50.py" in preview.body

    def test_overflow_lines_are_limited(self) -> None:
        preview = _preview(self._n_issues(_MAX_INLINE_COMMENTS + _MAX_OVERFLOW_BODY_LINES + 7))
        assert "_...and 7 more not shown._" in preview.body

    def test_summary_counts_overflow(self) -> None:
        total = _MAX_INLINE_COMMENTS + 3
        preview = _preview(self._n_issues(total))
        assert f"{total} finding(s)" in preview.body

    def test_under_cap_posts_all_inline(self) -> None:
        preview = _preview(self._n_issues(5))
        assert len(preview.inline_comments) == 5
        assert "capped" not in preview.body


class TestCoverage:
    @staticmethod
    def _report(**file_kwargs) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feature/x",
            base_branch="origin/main",
            files=[FileReview(file_path="src/f.py", **file_kwargs)],
        )

    def test_coverage_included_in_body_when_present(self) -> None:
        preview = _preview(self._report(coverage_pct=72.0, covered_lines=18, instrumented_changed_lines=25))
        assert "**72.0%**" in preview.body  # below 80, so bold
        assert "| 18 |" in preview.body
        assert "| 25 |" in preview.body
        assert "Coverage on changed lines" in preview.body

    def test_coverage_not_bold_at_or_above_80(self) -> None:
        preview = _preview(self._report(coverage_pct=90.0, covered_lines=9, instrumented_changed_lines=10))
        assert "| 90.0% |" in preview.body

    def test_coverage_absent_from_body_when_none(self) -> None:
        preview = _preview(self._report())
        assert "Coverage on changed lines" not in preview.body


class TestProjectLabelAndSha:
    @staticmethod
    def _report(project_label: str = "", head_sha: str = "", files: list[FileReview] | None = None) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            project_label=project_label,
            branch="feature/x",
            head_sha=head_sha,
            base_branch="origin/main",
            files=files or [],
        )

    @staticmethod
    def _one_issue() -> list[FileReview]:
        return [
            FileReview(
                file_path="src/f.py",
                issues=[ReviewIssue(rule="python:S1481", message="Remove unused variable", line=10)],
            )
        ]

    def test_label_included_in_no_findings_summary(self) -> None:
        preview = _preview(self._report(project_label="services/api"))
        assert "SonarQube [services/api]: no findings on or near changed lines." in preview.body

    def test_label_included_in_findings_summary(self) -> None:
        preview = _preview(self._report(project_label="services/api", files=self._one_issue()))
        assert "SonarQube [services/api]: 1 finding(s)" in preview.body

    def test_no_brackets_when_label_absent(self) -> None:
        preview = _preview(self._report())
        assert "SonarQube: no findings on or near changed lines." in preview.body
        assert "[" not in preview.body.splitlines()[0]

    def test_commit_included_in_no_findings_summary(self) -> None:
        preview = _preview(self._report(head_sha="abcdef1234567890"))
        assert "SonarQube: no findings on or near changed lines. (commit abcdef1)" in preview.body

    def test_commit_included_in_findings_summary(self) -> None:
        preview = _preview(self._report(head_sha="abcdef1234567890", files=self._one_issue()))
        assert preview.body.splitlines()[0].endswith("(commit abcdef1)")

    def test_no_commit_suffix_when_head_sha_absent(self) -> None:
        preview = _preview(self._report())
        assert "SonarQube: no findings on or near changed lines." in preview.body
        assert "(commit" not in preview.body

    def test_commit_included_in_fallback_body(self) -> None:
        assert "(commit abcdef1)" in build_fallback_body(
            self._report(head_sha="abcdef1234567890", files=self._one_issue())
        )
