"""Models for the review command."""

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class ReviewIssue(BaseModel):
    """Represents a single SonarQube issue found on a changed line."""

    model_config = {"extra": "ignore"}

    rule: str = Field(description="Rule identifier (e.g., 'python:S1481')")
    message: str = Field(description="Issue description")
    line: int = Field(description="Line number where the issue occurs")
    severity: str = Field(default="INFO", description="Issue severity (MAJOR, MINOR, CRITICAL, INFO)")
    doc_url: str | None = Field(default=None, description="Link to rule documentation")
    is_new_in_sonar: bool = Field(
        default=False,
        description=(
            "Whether this issue is new code per SonarQube. "
            "Currently always False — the source-line 'isNew' map is not fetched "
            "during filtering and no source_is_new_map is passed to filter_issues()."
        ),
    )
    root_cause: str | None = Field(
        default=None,
        description="HTML content of the root_cause description section from SonarQube rule API",
    )
    on_changed_line: bool = Field(
        default=True,
        description=(
            "True when the issue falls on a strictly changed line in the git diff. "
            "False when it falls only in the trailing context window added by DiffParser. "
            "GitHub rejects inline PR comments on trailing-context lines (422), so only "
            "issues with on_changed_line=True are posted inline."
        ),
    )


class DuplicationLocation(BaseModel):
    """Location of a duplicate code instance in another file."""

    model_config = {"extra": "ignore"}

    file_path: str = Field(description="Repo-relative path of the file containing the other instance")
    from_line: int = Field(description="First line of the duplicate block")
    to_line: int = Field(description="Last line of the duplicate block")


class ReviewDuplication(BaseModel):
    """Active duplication in the temp project that intersects modified lines (Feature 1)."""

    model_config = {"extra": "ignore"}

    from_line: int = Field(description="First line of the duplicated block in this file")
    to_line: int = Field(description="Last line of the duplicated block in this file")
    anchor_line: int | None = Field(
        default=None,
        description=(
            "First changed line within the duplicated block, used as the GitHub PR comment anchor. "
            "Falls back to from_line when not set (for backwards-compatible deserialized reports)."
        ),
    )
    other_locations: list[DuplicationLocation] = Field(
        default_factory=list,
        description="Other files/lines where this block is duplicated",
    )


class ResolvedDuplication(BaseModel):
    """A block that was duplicated in main was modified; other instances may need updating (Feature 2)."""

    model_config = {"extra": "ignore"}

    main_from_line: int = Field(description="Start of the original duplicated block in main")
    main_to_line: int = Field(description="End of the original duplicated block in main")
    other_locations: list[DuplicationLocation] = Field(
        default_factory=list,
        description="Other instances in main that may still need updating",
    )
    anchor_new_line: int = Field(description="New-file line number used as the PR comment anchor")


class FileReview(BaseModel):
    """Review results for a single file."""

    model_config = {"extra": "ignore"}

    file_path: str = Field(description="Path to the file relative to project root")
    issues: list[ReviewIssue] = Field(default_factory=list, description="Issues found in this file")
    duplications: list[ReviewDuplication] = Field(
        default_factory=list,
        description="Active duplication blocks intersecting changed lines",
    )
    resolved_duplications: list[ResolvedDuplication] = Field(
        default_factory=list,
        description="Blocks duplicated in main that were modified; other instances may need updating",
    )
    coverage_pct: float | None = Field(
        default=None,
        description="Percentage of instrumented changed lines covered by tests.",
    )
    covered_lines: int = Field(default=0, description="Instrumented changed lines that were covered.")
    instrumented_changed_lines: int = Field(
        default=0, description="Changed lines that could be covered (lineHits not None)."
    )


class FileDiagnostics(BaseModel):
    """Per-file diagnostic data written to review.json.

    # TODO: remove this class and all references once the line-filter pipeline
    # is considered stable — it is only here to aid debugging.


    Captures the intermediate state at each pipeline stage so that a mismatch
    (0 issues when some are expected) can be traced to the exact failure point:

    - ``changed_lines`` empty → the changed-lines map lookup missed this file
      (path mismatch between DiffParser and BranchAnalyzer keys).
    - ``sonar_issue_lines`` empty → SonarQube returned no issues for the file
      (component-path mismatch or file not scanned).
    - Issue line present in ``sonar_issue_lines`` but absent from ``changed_lines``
      → line filter too strict (trailing-context fix may not have been enough).
    """

    file_path: str = Field(description="Path passed to SonarQube component query")
    lookup_key: str = Field(description="Key used to look up this file in the diff changed-lines map")
    changed_lines: list[int] = Field(
        default_factory=list,
        description="Line numbers from git diff (sorted); empty = path lookup missed or no diff for file",
    )
    sonar_issue_count: int = Field(default=0, description="Issues from SonarQube before line filtering")
    sonar_issue_lines: list[int] = Field(
        default_factory=list,
        description="Line numbers of those issues (sorted)",
    )
    active_dup_api_status: str = Field(
        default="",
        description=(
            "Outcome of /api/duplications/show against the temp project. "
            "'' = not attempted; 'skipped_no_changed_lines'; 'ok'; "
            "'component_not_found'; 'api_error:<msg>'; 'error:<type>:<msg>'"
        ),
    )
    active_dup_groups_found: int = Field(
        default=0,
        description="Duplication groups returned by the temp-project API (0 when status != 'ok')",
    )
    active_dup_target_ref_found: bool = Field(
        default=False,
        description="Whether the target file's ref was located in the duplications response",
    )
    active_dup_blocks_intersecting: int = Field(
        default=0,
        description="Duplication blocks whose line range intersects the changed lines",
    )
    resolved_dup_api_status: str = Field(
        default="",
        description="Outcome of /api/duplications/show against the main project (same values as active_dup_api_status)",
    )
    resolved_dup_groups_found: int = Field(
        default=0,
        description="Duplication groups returned by the main-project API",
    )


class ReviewResult(BaseModel):
    """Complete review result for a branch."""

    model_config = {"extra": "ignore"}

    project_key: str = Field(description="SonarQube project key")
    project_label: str = Field(
        default="",
        description=(
            "Human-readable label identifying which SonarQube project this review run analyzed "
            "(the temporary analysis project's display name). Included in PR comments to "
            "distinguish runs when multiple reviews target different projects."
        ),
    )
    branch: str = Field(description="Current branch name")
    head_sha: str = Field(default="", description="Full HEAD commit SHA the review was generated against")
    base_branch: str = Field(description="Base branch for comparison (e.g., 'origin/main')")
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Timestamp when the review was generated",
    )
    files: list[FileReview] = Field(default_factory=list, description="Per-file review results")
    files_analyzed: int = Field(
        default=0,
        description="Total number of modified files that were analyzed (including files with no findings)",
    )
    diagnostics: list[FileDiagnostics] = Field(
        default_factory=list,
        description="Per-file diagnostic data for debugging line-filter behaviour",
    )
    diff_files_found: int = Field(
        default=0,
        description="Number of files the git diff parser found with changes",
    )
    diff_map_keys: list[str] = Field(
        default_factory=list,
        description="Keys from the git diff changed-lines map (compare against diagnostics.lookup_key to spot mismatches)",
    )
    diff_output_sample: str = Field(
        default="",
        description="First 500 chars of raw git diff output (empty = diff returned nothing; helps distinguish empty diff from parse failure)",
    )

    @property
    def total_issues(self) -> int:
        """Return the total number of issues across all files."""
        return sum(len(f.issues) for f in self.files)

    @property
    def total_duplications(self) -> int:
        """Return the total number of active and resolved duplication findings."""
        return sum(len(f.duplications) + len(f.resolved_duplications) for f in self.files)
