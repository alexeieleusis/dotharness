"""Tests for review command models."""

from datetime import datetime, timezone

from dotharness.vibe_heal.review.models import (
    DuplicationLocation,
    FileReview,
    ResolvedDuplication,
    ReviewDuplication,
    ReviewIssue,
    ReviewResult,
)


class TestReviewIssue:
    """Tests for ReviewIssue model."""

    def test_create_issue_with_all_fields(self) -> None:
        """Test creating a review issue with all fields."""
        issue = ReviewIssue(
            rule="python:S1481",
            message="Remove this unused import",
            line=42,
            severity="MAJOR",
            doc_url="https://next.sonarqube.com/sonarqube/coding_rules?open=python%3AS1481",
            is_new_in_sonar=True,
        )

        assert issue.rule == "python:S1481"
        assert issue.message == "Remove this unused import"
        assert issue.line == 42
        assert issue.severity == "MAJOR"
        assert issue.doc_url == "https://next.sonarqube.com/sonarqube/coding_rules?open=python%3AS1481"
        assert issue.is_new_in_sonar is True

    def test_create_issue_with_defaults(self) -> None:
        """Test creating a review issue with default values."""
        issue = ReviewIssue(
            rule="python:S1192",
            message="Unused variable",
            line=10,
        )

        assert issue.rule == "python:S1192"
        assert issue.severity == "INFO"
        assert issue.doc_url is None
        assert issue.is_new_in_sonar is False

    def test_serialization_round_trip(self) -> None:
        """Test that a ReviewIssue serializes and deserializes correctly."""
        original = ReviewIssue(
            rule="typescript:S3801",
            message="Refactor this function",
            line=100,
            severity="CRITICAL",
            doc_url="https://example.com/rule",
            is_new_in_sonar=True,
        )

        data = original.model_dump()
        restored = ReviewIssue(**data)

        assert restored.rule == original.rule
        assert restored.message == original.message
        assert restored.line == original.line
        assert restored.severity == original.severity
        assert restored.doc_url == original.doc_url
        assert restored.is_new_in_sonar == original.is_new_in_sonar

    def test_json_serialization_round_trip(self) -> None:
        """Test JSON serialization and deserialization."""
        original = ReviewIssue(
            rule="python:S1067",
            message="Uppercase in underscore",
            line=5,
            severity="MINOR",
            is_new_in_sonar=False,
        )

        json_str = original.model_dump_json()
        restored = ReviewIssue.model_validate_json(json_str)

        assert restored.rule == original.rule
        assert restored.message == original.message
        assert restored.line == original.line
        assert restored.severity == original.severity
        assert restored.doc_url == original.doc_url
        assert restored.is_new_in_sonar == original.is_new_in_sonar

    def test_extra_fields_ignored(self) -> None:
        """Test that extra fields from API are ignored."""
        issue = ReviewIssue(
            rule="python:S1481",
            message="Test",
            line=1,
            extra_field="should be ignored",
            another_field=123,
        )

        assert issue.rule == "python:S1481"


class TestFileReview:
    """Tests for FileReview model."""

    def test_create_file_review_with_issues(self) -> None:
        """Test creating a file review with multiple issues."""
        issues = [
            ReviewIssue(rule="python:S1481", message="Issue 1", line=10, severity="MAJOR"),
            ReviewIssue(rule="python:S1192", message="Issue 2", line=20, severity="MINOR"),
        ]

        review = FileReview(file_path="src/main.py", issues=issues)

        assert review.file_path == "src/main.py"
        assert len(review.issues) == 2
        assert review.issues[0].rule == "python:S1481"
        assert review.issues[1].line == 20

    def test_create_file_review_empty(self) -> None:
        """Test creating a file review with no issues."""
        review = FileReview(file_path="src/clean.py", issues=[])

        assert review.file_path == "src/clean.py"
        assert review.issues == []

    def test_serialization_round_trip(self) -> None:
        """Test that a FileReview serializes and deserializes correctly."""
        original = FileReview(
            file_path="src/utils.py",
            issues=[
                ReviewIssue(
                    rule="python:S1481",
                    message="Remove unused",
                    line=5,
                    severity="MAJOR",
                    is_new_in_sonar=True,
                ),
            ],
        )

        data = original.model_dump()
        restored = FileReview(**data)

        assert restored.file_path == original.file_path
        assert len(restored.issues) == 1
        assert restored.issues[0].rule == original.issues[0].rule
        assert restored.issues[0].is_new_in_sonar == original.issues[0].is_new_in_sonar

    def test_json_serialization_round_trip(self) -> None:
        """Test JSON serialization and deserialization."""
        original = FileReview(
            file_path="src/app.ts",
            issues=[
                ReviewIssue(rule="typescript:S3801", message="Refactor", line=50),
                ReviewIssue(rule="typescript:S100", message="Too long", line=80),
            ],
        )

        json_str = original.model_dump_json()
        restored = FileReview.model_validate_json(json_str)

        assert restored.file_path == original.file_path
        assert len(restored.issues) == 2
        assert restored.issues[0].rule == "typescript:S3801"
        assert restored.issues[1].line == 80


class TestReviewResult:
    """Tests for ReviewResult model."""

    def test_create_review_result(self) -> None:
        """Test creating a review result with all fields."""
        files = [
            FileReview(
                file_path="src/a.py",
                issues=[ReviewIssue(rule="python:S1481", message="Issue", line=1)],
            ),
            FileReview(
                file_path="src/b.py",
                issues=[
                    ReviewIssue(rule="python:S1192", message="Issue A", line=10),
                    ReviewIssue(rule="python:S1067", message="Issue B", line=20),
                ],
            ),
        ]

        result = ReviewResult(
            project_key="my-project",
            branch="feature/test",
            base_branch="origin/main",
            generated_at=datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
            files=files,
        )

        assert result.project_key == "my-project"
        assert result.branch == "feature/test"
        assert result.base_branch == "origin/main"
        assert len(result.files) == 2
        assert result.total_issues == 3

    def test_total_issues_empty(self) -> None:
        """Test total_issues returns 0 when no files."""
        result = ReviewResult(
            project_key="my-project",
            branch="main",
            base_branch="origin/main",
            files=[],
        )

        assert result.total_issues == 0

    def test_total_issues_files_with_no_issues(self) -> None:
        """Test total_issues returns 0 when files exist but have no issues."""
        result = ReviewResult(
            project_key="my-project",
            branch="main",
            base_branch="origin/main",
            files=[
                FileReview(file_path="src/clean.py", issues=[]),
                FileReview(file_path="src/also_clean.py", issues=[]),
            ],
        )

        assert result.total_issues == 0

    def test_generated_at_defaults_to_now(self) -> None:
        """Test that generated_at defaults to current UTC time."""
        before = datetime.now(timezone.utc)
        result = ReviewResult(
            project_key="my-project",
            branch="main",
            base_branch="origin/main",
        )
        after = datetime.now(timezone.utc)

        assert before <= result.generated_at <= after
        assert result.generated_at.tzinfo is not None

    def test_serialization_round_trip(self) -> None:
        """Test that a ReviewResult serializes and deserializes correctly."""
        generated_at = datetime(2025, 6, 15, 9, 30, 0, tzinfo=timezone.utc)
        original = ReviewResult(
            project_key="test-project",
            branch="dev",
            base_branch="origin/main",
            generated_at=generated_at,
            files=[
                FileReview(
                    file_path="src/x.py",
                    issues=[
                        ReviewIssue(
                            rule="python:S1481",
                            message="Unused",
                            line=3,
                            severity="MAJOR",
                            is_new_in_sonar=True,
                            doc_url="https://example.com",
                        ),
                    ],
                ),
            ],
        )

        data = original.model_dump()
        restored = ReviewResult(**data)

        assert restored.project_key == original.project_key
        assert restored.branch == original.branch
        assert restored.base_branch == original.base_branch
        assert restored.generated_at == original.generated_at
        assert restored.total_issues == original.total_issues
        assert len(restored.files) == 1
        assert restored.files[0].issues[0].is_new_in_sonar is True

    def test_json_serialization_round_trip(self) -> None:
        """Test JSON serialization and deserialization."""
        generated_at = datetime(2025, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
        original = ReviewResult(
            project_key="proj",
            branch="feat",
            base_branch="origin/main",
            generated_at=generated_at,
            files=[
                FileReview(
                    file_path="src/y.ts",
                    issues=[ReviewIssue(rule="ts:S1", message="M", line=1)],
                ),
            ],
        )

        json_str = original.model_dump_json()
        restored = ReviewResult.model_validate_json(json_str)

        assert restored.project_key == original.project_key
        assert restored.branch == original.branch
        assert restored.generated_at == original.generated_at
        assert restored.total_issues == 1


class TestDuplicationModels:
    """Tests for duplication-related review models."""

    def test_duplication_location_creation(self) -> None:
        """Test creating a DuplicationLocation."""
        loc = DuplicationLocation(file_path="src/utils.py", from_line=10, to_line=25)

        assert loc.file_path == "src/utils.py"
        assert loc.from_line == 10
        assert loc.to_line == 25

    def test_review_duplication_defaults(self) -> None:
        """Test ReviewDuplication defaults to empty other_locations."""
        dup = ReviewDuplication(from_line=5, to_line=20)

        assert dup.from_line == 5
        assert dup.to_line == 20
        assert dup.other_locations == []

    def test_review_duplication_with_locations(self) -> None:
        """Test ReviewDuplication with other_locations populated."""
        dup = ReviewDuplication(
            from_line=10,
            to_line=30,
            other_locations=[
                DuplicationLocation(file_path="src/a.py", from_line=50, to_line=70),
                DuplicationLocation(file_path="src/b.py", from_line=1, to_line=21),
            ],
        )

        assert len(dup.other_locations) == 2
        assert dup.other_locations[0].file_path == "src/a.py"

    def test_resolved_duplication_creation(self) -> None:
        """Test creating a ResolvedDuplication."""
        res = ResolvedDuplication(
            main_from_line=45,
            main_to_line=60,
            other_locations=[DuplicationLocation(file_path="src/old.py", from_line=100, to_line=115)],
            anchor_new_line=48,
        )

        assert res.main_from_line == 45
        assert res.main_to_line == 60
        assert res.anchor_new_line == 48
        assert len(res.other_locations) == 1

    def test_file_review_with_duplications_round_trip(self) -> None:
        """FileReview serializes and deserializes with all duplication fields."""
        original = FileReview(
            file_path="src/main.py",
            issues=[ReviewIssue(rule="python:S1", message="X", line=5)],
            duplications=[
                ReviewDuplication(
                    from_line=10,
                    to_line=20,
                    other_locations=[DuplicationLocation(file_path="src/other.py", from_line=50, to_line=60)],
                )
            ],
            resolved_duplications=[
                ResolvedDuplication(
                    main_from_line=30,
                    main_to_line=40,
                    other_locations=[DuplicationLocation(file_path="src/third.py", from_line=1, to_line=11)],
                    anchor_new_line=31,
                )
            ],
        )

        json_str = original.model_dump_json()
        restored = FileReview.model_validate_json(json_str)

        assert restored.file_path == original.file_path
        assert len(restored.duplications) == 1
        assert restored.duplications[0].from_line == 10
        assert restored.duplications[0].other_locations[0].file_path == "src/other.py"
        assert len(restored.resolved_duplications) == 1
        assert restored.resolved_duplications[0].main_from_line == 30
        assert restored.resolved_duplications[0].anchor_new_line == 31

    def test_file_review_without_duplication_fields_loads_cleanly(self) -> None:
        """Old FileReview JSON (without duplication fields) loads with empty defaults."""
        old_json = '{"file_path": "src/file.py", "issues": []}'

        restored = FileReview.model_validate_json(old_json)

        assert restored.file_path == "src/file.py"
        assert restored.duplications == []
        assert restored.resolved_duplications == []

    def test_review_result_total_duplications(self) -> None:
        """ReviewResult.total_duplications counts across all files."""
        result = ReviewResult(
            project_key="proj",
            branch="feat",
            base_branch="origin/main",
            files=[
                FileReview(
                    file_path="src/a.py",
                    duplications=[ReviewDuplication(from_line=1, to_line=10)],
                    resolved_duplications=[ResolvedDuplication(main_from_line=20, main_to_line=30, anchor_new_line=22)],
                ),
                FileReview(
                    file_path="src/b.py",
                    duplications=[
                        ReviewDuplication(from_line=5, to_line=15),
                        ReviewDuplication(from_line=40, to_line=50),
                    ],
                ),
            ],
        )

        assert result.total_duplications == 4  # 1+1 + 2+0


class TestFileReviewCoverageFields:
    def test_coverage_fields_default_to_none_and_zero(self) -> None:
        fr = FileReview(file_path="src/f.py")
        assert fr.coverage_pct is None
        assert fr.covered_lines == 0
        assert fr.instrumented_changed_lines == 0

    def test_coverage_fields_serialise_to_dict(self) -> None:
        fr = FileReview(file_path="src/f.py", coverage_pct=72.5, covered_lines=18, instrumented_changed_lines=25)
        data = fr.model_dump()
        assert data["coverage_pct"] == 72.5
        assert data["covered_lines"] == 18
        assert data["instrumented_changed_lines"] == 25

    def test_coverage_fields_round_trip(self) -> None:
        fr = FileReview(file_path="src/f.py", coverage_pct=50.0, covered_lines=5, instrumented_changed_lines=10)
        restored = FileReview.model_validate(fr.model_dump())
        assert restored.coverage_pct == 50.0
        assert restored.covered_lines == 5
        assert restored.instrumented_changed_lines == 10

    def test_old_json_without_coverage_fields_loads_safely(self) -> None:
        old_data = {"file_path": "src/f.py", "issues": [], "duplications": [], "resolved_duplications": []}
        fr = FileReview.model_validate(old_data)
        assert fr.coverage_pct is None
        assert fr.covered_lines == 0
