"""Tests for the Reporter module."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dotharness.vibe_heal.review.models import (
    DuplicationLocation,
    FileReview,
    ResolvedDuplication,
    ReviewDuplication,
    ReviewIssue,
    ReviewResult,
)
from dotharness.vibe_heal.review.reporter import (
    default_report_dir,
    format_coverage_table,
    load_report,
    load_report_from_path,
    write_report_to_file,
    write_reports,
)


def _make_result() -> ReviewResult:
    """Create a sample ReviewResult for testing."""
    return ReviewResult(
        project_key="my-project",
        branch="feature/test",
        base_branch="origin/main",
        generated_at=datetime(2025, 10, 24, 14, 30, 0, tzinfo=UTC),
        files=[
            FileReview(
                file_path="src/example.py",
                issues=[
                    ReviewIssue(rule="python:S1481", message="Remove unused variable", line=10, severity="MAJOR"),
                    ReviewIssue(rule="python:S1192", message="Useless import", line=25, severity="MINOR"),
                ],
            ),
            FileReview(
                file_path="src/utils.py",
                issues=[
                    ReviewIssue(
                        rule="python:S100",
                        message="Syntax error",
                        line=5,
                        severity="CRITICAL",
                        doc_url="https://rules.example.com/S100",
                    ),
                ],
            ),
        ],
    )


def _make_empty_result() -> ReviewResult:
    """Create an empty ReviewResult for testing."""
    return ReviewResult(
        project_key="empty-project",
        branch="main",
        base_branch="origin/main",
        generated_at=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
        files=[],
    )


class TestDefaultReportDir:
    """Tests for default_report_dir."""

    def test_returns_path_with_project_and_branch(self) -> None:
        result = default_report_dir("my-project", "feature/x")
        posix = result.as_posix()
        assert "vibe-heal" in posix
        assert "reviews" in posix
        assert "my-project" in posix
        assert "feature/x" in posix

    def test_expands_tilde_home(self) -> None:
        result = default_report_dir("proj", "main")
        assert result.is_absolute()


class TestWriteReports:
    """Tests for write_reports."""

    def test_creates_json_and_markdown_files(self, tmp_path: Path) -> None:
        result = _make_result()
        write_reports(result, tmp_path)
        assert (tmp_path / "review.json").exists()
        assert (tmp_path / "review.md").exists()

    def test_creates_directory_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "dir"
        result = _make_result()
        write_reports(result, nested)
        assert nested.is_dir()
        assert (nested / "review.json").exists()
        assert (nested / "review.md").exists()

    def test_json_round_trip(self, tmp_path: Path) -> None:
        result = _make_result()
        write_reports(result, tmp_path)
        loaded = load_report(tmp_path)
        assert loaded.project_key == result.project_key
        assert loaded.branch == result.branch
        assert loaded.base_branch == result.base_branch
        assert loaded.total_issues == result.total_issues
        assert len(loaded.files) == len(result.files)
        assert loaded.files[0].file_path == "src/example.py"
        assert loaded.files[0].issues[0].rule == "python:S1481"
        assert loaded.files[0].issues[0].line == 10

    def test_empty_result_produces_valid_files(self, tmp_path: Path) -> None:
        result = _make_empty_result()
        write_reports(result, tmp_path)
        assert (tmp_path / "review.json").exists()
        assert (tmp_path / "review.md").exists()
        loaded = load_report(tmp_path)
        assert loaded.total_issues == 0
        assert loaded.files == []

    def test_markdown_contains_summary(self, tmp_path: Path) -> None:
        result = _make_result()
        write_reports(result, tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "my-project" in md
        assert "feature/test" in md
        assert "3" in md  # total issues count

    def test_markdown_contains_file_tables(self, tmp_path: Path) -> None:
        result = _make_result()
        write_reports(result, tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "src/example.py" in md
        assert "src/utils.py" in md
        # Table header
        assert "Rule" in md
        assert "Message" in md
        assert "Line" in md
        assert "Severity" in md
        # Table formatting (markdown pipes)
        assert "|---" in md or "|--" in md

    def test_markdown_contains_issue_details(self, tmp_path: Path) -> None:
        result = _make_result()
        write_reports(result, tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "python:S1481" in md
        assert "Remove unused variable" in md
        assert "MAJOR" in md
        assert "CRITICAL" in md
        assert "https://rules.example.com/S100" in md

    def test_markdown_empty_result_has_no_tables(self, tmp_path: Path) -> None:
        result = _make_empty_result()
        write_reports(result, tmp_path)
        md = (tmp_path / "review.md").read_text()
        # Should not have table rows
        assert "python:" not in md

    def test_custom_path_override(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "custom" / "reports"
        result = _make_result()
        write_reports(result, custom_dir)
        assert (custom_dir / "review.json").exists()
        assert (custom_dir / "review.md").exists()
        loaded = load_report(custom_dir)
        assert loaded.total_issues == 3

    def test_markdown_rule_descriptions_collapsed_section(self, tmp_path: Path) -> None:
        """root_cause on ReviewIssue renders as one <details> block per unique rule."""
        result = ReviewResult(
            project_key="my-project",
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
                            root_cause="<p>Unused variables clutter code.</p>",
                        ),
                        # Same rule — should produce only one <details> block
                        ReviewIssue(
                            rule="python:S1481",
                            message="Another unused variable",
                            line=20,
                            severity="MAJOR",
                            root_cause="<p>Unused variables clutter code.</p>",
                        ),
                        # Different rule — should produce a second <details> block
                        ReviewIssue(
                            rule="python:S1192",
                            message="Define a constant",
                            line=30,
                            severity="MINOR",
                            root_cause="<p>Magic numbers reduce readability.</p>",
                        ),
                        # No root_cause — should produce no <details> block
                        ReviewIssue(rule="python:S999", message="Other issue", line=40, severity="INFO"),
                    ],
                )
            ],
        )
        write_reports(result, tmp_path)
        md = (tmp_path / "review.md").read_text()

        assert md.count("<details>") == 2
        assert md.count("</details>") == 2
        assert "python:S1481 — why this matters" in md
        assert "python:S1192 — why this matters" in md
        assert "<p>Unused variables clutter code.</p>" in md
        assert "<p>Magic numbers reduce readability.</p>" in md
        assert "python:S999" not in md.split("<details>")[1] if "<details>" in md else True


class TestWriteReportToFile:
    def test_writes_json_and_markdown(self, tmp_path: Path) -> None:
        write_report_to_file(_make_result(), tmp_path / "out" / "report.json")
        assert (tmp_path / "out" / "report.json").exists()
        assert (tmp_path / "out" / "report.md").exists()

    def test_md_suffix_raises_without_writing(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"\.md"):
            write_report_to_file(_make_result(), tmp_path / "report.md")
        assert not (tmp_path / "report.md").exists()

    def test_markdown_uses_same_stem_not_review_md(self, tmp_path: Path) -> None:
        write_report_to_file(_make_result(), tmp_path / "r.json")
        assert (tmp_path / "r.md").exists()
        assert not (tmp_path / "review.md").exists()

    def test_creates_missing_parent_dir(self, tmp_path: Path) -> None:
        write_report_to_file(_make_result(), tmp_path / "a" / "b" / "r.json")
        assert (tmp_path / "a" / "b" / "r.json").exists()
        assert (tmp_path / "a" / "b" / "r.md").exists()


class TestLoadReport:
    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"review\.json"):
            load_report(tmp_path)

    def test_malformed_json_raises_value_error_with_path(self, tmp_path: Path) -> None:
        (tmp_path / "review.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match=r"review\.json"):
            load_report(tmp_path)

    def test_round_trip(self, tmp_path: Path) -> None:
        original = _make_result()
        write_reports(original, tmp_path)
        assert load_report(tmp_path) == original


class TestLoadReportFromPath:
    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"missing\.json"):
            load_report_from_path(tmp_path / "missing.json")

    def test_malformed_json_raises_value_error_with_path(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match=r"bad\.json"):
            load_report_from_path(path)

    def test_round_trip_with_write_report_to_file(self, tmp_path: Path) -> None:
        original = _make_result()
        write_report_to_file(original, tmp_path / "out" / "r.json")
        assert load_report_from_path(tmp_path / "out" / "r.json") == original


class TestMarkdownRendering:
    def test_message_pipe_and_newline_escaped(self, tmp_path: Path) -> None:
        result = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            generated_at=datetime(2025, 1, 1, tzinfo=UTC),
            files=[
                FileReview(
                    file_path="src/a.py",
                    issues=[ReviewIssue(rule="r:1", message="a|b\nc", line=1, severity="MAJOR")],
                )
            ],
        )
        write_reports(result, tmp_path)
        assert "| a\\|b c |" in (tmp_path / "review.md").read_text()

    def test_duplication_sections_rendered(self, tmp_path: Path) -> None:
        other = DuplicationLocation(file_path="src/other.py", from_line=3, to_line=9)
        result = ReviewResult(
            project_key="p",
            branch="b",
            base_branch="main",
            generated_at=datetime(2025, 1, 1, tzinfo=UTC),
            files=[
                FileReview(
                    file_path="src/a.py",
                    duplications=[ReviewDuplication(from_line=1, to_line=7, other_locations=[other])],
                    resolved_duplications=[
                        ResolvedDuplication(
                            main_from_line=10, main_to_line=20, other_locations=[other], anchor_new_line=12
                        )
                    ],
                )
            ],
        )
        write_reports(result, tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "### Active Duplications" in md
        assert "### Resolved Duplications" in md
        assert "lines 1-7" in md
        assert "`src/other.py` lines 3-9" in md
        assert "lines 10-20" in md
        assert "**Duplication findings: 2**" in md


class TestCoverageInMarkdown:
    def _result(self, **kwargs) -> ReviewResult:
        return ReviewResult(
            project_key="p",
            branch="feature/x",
            base_branch="origin/main",
            files=[FileReview(file_path="src/f.py", **kwargs)],
        )

    def test_coverage_pct_rendered_when_present(self, tmp_path: Path) -> None:
        write_reports(self._result(coverage_pct=72.0, covered_lines=18, instrumented_changed_lines=25), tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "**72.0%**" in md  # 72.0 < 80, so bold
        assert "| 18 |" in md
        assert "| 25 |" in md
        assert "Coverage on changed lines" in md

    def test_coverage_zero_percent_rendered(self, tmp_path: Path) -> None:
        write_reports(self._result(coverage_pct=0.0, covered_lines=0, instrumented_changed_lines=5), tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "**0.0%**" in md  # 0.0 < 80, so bold
        assert "| 0 |" in md
        assert "| 5 |" in md

    def test_coverage_100_percent_rendered(self, tmp_path: Path) -> None:
        write_reports(self._result(coverage_pct=100.0, covered_lines=10, instrumented_changed_lines=10), tmp_path)
        md = (tmp_path / "review.md").read_text()
        assert "100.0%" in md

    def test_no_coverage_line_when_coverage_pct_none(self, tmp_path: Path) -> None:
        write_reports(self._result(), tmp_path)  # coverage_pct defaults to None
        md = (tmp_path / "review.md").read_text()
        assert "## Coverage on changed lines" not in md

    def test_no_issues_label_present_when_only_coverage_data(self, tmp_path: Path) -> None:
        """A file with only coverage data (no issues/dups) shows 'No issues.' in file section."""
        write_reports(
            self._result(coverage_pct=80.0, covered_lines=8, instrumented_changed_lines=10),
            tmp_path,
        )
        md = (tmp_path / "review.md").read_text()
        assert "No issues." in md  # guard condition will change — coverage-only file shows "No issues."
        assert "80.0%" in md  # still appears in the summary table


class TestFormatCoverageTable:
    def _make_fr(
        self,
        file_path: str = "src/foo.py",
        covered_lines: int = 8,
        instrumented_changed_lines: int = 10,
        coverage_pct: float = 80.0,
    ) -> FileReview:
        return FileReview(
            file_path=file_path,
            issues=[],
            duplications=[],
            resolved_duplications=[],
            covered_lines=covered_lines,
            instrumented_changed_lines=instrumented_changed_lines,
            coverage_pct=coverage_pct,
        )

    def test_returns_empty_string_for_empty_list(self) -> None:
        assert format_coverage_table([]) == ""

    def test_header_row_present(self) -> None:
        table = format_coverage_table([self._make_fr()])
        assert "| File | Covered Lines | Instrumented Changed Lines | Coverage % |" in table

    def test_alignment_row_present(self) -> None:
        table = format_coverage_table([self._make_fr()])
        assert "| :--- | ---: | ---: | ---: |" in table

    def test_covered_and_instrumented_in_correct_columns(self) -> None:
        table = format_coverage_table([
            self._make_fr(covered_lines=18, instrumented_changed_lines=25, coverage_pct=72.0)
        ])
        assert "| 18 |" in table
        assert "| 25 |" in table

    def test_coverage_pct_bold_when_below_80(self) -> None:
        table = format_coverage_table([self._make_fr(coverage_pct=79.9)])
        assert "**79.9%**" in table

    def test_coverage_pct_not_bold_at_exactly_80(self) -> None:
        table = format_coverage_table([self._make_fr(coverage_pct=80.0)])
        assert "**80.0%**" not in table
        assert "80.0%" in table

    def test_coverage_pct_not_bold_above_80(self) -> None:
        table = format_coverage_table([self._make_fr(coverage_pct=95.5)])
        assert "**95.5%**" not in table
        assert "95.5%" in table

    def test_coverage_pct_one_decimal_place(self) -> None:
        table = format_coverage_table([self._make_fr(coverage_pct=72.0)])
        assert "72.0%" in table

    def test_file_path_in_table(self) -> None:
        table = format_coverage_table([self._make_fr(file_path="src/components/foo.tsx")])
        assert "src/components/foo.tsx" in table

    def test_returns_empty_string_when_all_coverage_none(self) -> None:
        fr = self._make_fr()
        fr.coverage_pct = None
        assert format_coverage_table([fr]) == ""
