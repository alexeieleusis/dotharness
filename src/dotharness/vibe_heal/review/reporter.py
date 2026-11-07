"""Reporter: writes review results as JSON and markdown."""

from __future__ import annotations

from pathlib import Path

from dotharness.vibe_heal.review.models import (
    FileReview,
    ResolvedDuplication,
    ReviewDuplication,
    ReviewIssue,
    ReviewResult,
)


def default_report_dir(project_key: str, branch: str) -> Path:
    """Return the default report directory for a project and branch.

    Path format: ~/.vibe-heal/reviews/<project-key>/<branch>/
    """
    return Path.home() / ".vibe-heal" / "reviews" / project_key / branch


def write_reports(result: ReviewResult, report_dir: Path) -> None:
    """Write review.json and review.md to the given directory.

    Creates the directory (and parents) if it doesn't exist.
    """
    report_dir.mkdir(parents=True, exist_ok=True)
    _write_json(result, report_dir / "review.json")
    _write_markdown(result, report_dir / "review.md")


def write_report_to_file(result: ReviewResult, json_path: Path) -> None:
    """Write review JSON to an exact path and markdown alongside it (same stem, .md).

    Creates the parent directory if it doesn't exist.

    Raises:
        ValueError: If ``json_path`` already ends in ``.md``, since the markdown
            companion would overwrite the JSON report.
    """
    md_path = json_path.parent / (json_path.stem + ".md")
    if md_path == json_path:
        msg = f"Report file must not end in .md (markdown would overwrite the JSON report): {json_path}"
        raise ValueError(msg)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(result, json_path)
    _write_markdown(result, md_path)


def load_report(report_dir: Path) -> ReviewResult:
    """Load a ReviewResult from review.json in the given directory."""
    json_path = report_dir / "review.json"
    try:
        data = json_path.read_text(encoding="utf-8")
    except OSError as e:
        msg = f"Cannot read report file: {json_path}"
        raise FileNotFoundError(msg) from e
    try:
        return ReviewResult.model_validate_json(data)
    except Exception as e:
        msg = f"Malformed report file: {json_path}"
        raise ValueError(msg) from e


def load_report_from_path(json_path: Path) -> ReviewResult:
    """Load a ReviewResult from a specific JSON file path."""
    try:
        data = json_path.read_text(encoding="utf-8")
    except OSError as e:
        msg = f"Cannot read report file: {json_path}"
        raise FileNotFoundError(msg) from e
    try:
        return ReviewResult.model_validate_json(data)
    except Exception as e:
        msg = f"Malformed report file: {json_path}"
        raise ValueError(msg) from e


def _write_json(result: ReviewResult, path: Path) -> None:
    """Serialize ReviewResult to JSON."""
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")


def _format_rule_display(issue: ReviewIssue) -> str:
    """Format a rule identifier, optionally as a link."""
    if issue.doc_url:
        return f"[{issue.rule}]({issue.doc_url})"
    return issue.rule


def _format_message(msg: str) -> str:
    """Escape characters that would break a markdown table cell."""
    return msg.replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _render_issues_table(issues: list[ReviewIssue]) -> list[str]:
    """Render the issues section as markdown table rows."""
    lines = ["| Rule | Message | Line | Severity |", "|------|---------|------|----------|"]
    for issue in issues:
        lines.append(
            f"| {_format_rule_display(issue)} | {_format_message(issue.message)} | {issue.line} | {issue.severity} |"
        )
    lines.append("")
    return lines


def _render_rule_descriptions(issues: list[ReviewIssue]) -> list[str]:
    """Render one collapsed <details> per unique rule that has a root_cause."""
    lines: list[str] = []
    seen: set[str] = set()
    for issue in issues:
        if issue.root_cause and issue.rule not in seen:
            seen.add(issue.rule)
            lines.extend([
                "<details>",
                "",
                f"<summary>{issue.rule} — why this matters</summary>",
                "",
                issue.root_cause,
                "",
                "</details>",
                "",
            ])
    return lines


def _render_duplications(duplications: list[ReviewDuplication]) -> list[str]:
    """Render the active duplications section."""
    lines = ["### Active Duplications", "", "| Block (this file) | Duplicated in |", "|---|---|"]
    for dup in duplications:
        locations = ", ".join(f"`{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in dup.other_locations)
        lines.append(f"| lines {dup.from_line}-{dup.to_line} | {locations} |")
    lines.append("")
    return lines


def _render_resolved_duplications(resolved: list[ResolvedDuplication], base_branch: str = "main") -> list[str]:
    """Render the resolved duplications section."""
    lines = [
        "### Resolved Duplications - check other instances",
        "",
        f"> You modified lines that were part of a duplicated block in `{base_branch}`. "
        "The duplication is no longer detected in this branch, but other instances may need updating.",
        "",
        f"| Block in `{base_branch}` | Other instances |",
        "|---|---|",
    ]
    for res in resolved:
        locations = ", ".join(f"`{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in res.other_locations)
        lines.append(f"| lines {res.main_from_line}-{res.main_to_line} | {locations} |")
    lines.append("")
    return lines


def format_coverage_table(file_reviews: list[FileReview]) -> str:
    """Render a Markdown table summarising coverage across files.

    Returns an empty string when *file_reviews* is empty or no file has a
    non-None ``coverage_pct``.  Coverage percentages below 80 % are rendered
    in bold to draw attention.
    """
    if not file_reviews:
        return ""
    header = "| File | Covered Lines | Instrumented Changed Lines | Coverage % |"
    alignment = "| :--- | ---: | ---: | ---: |"
    rows = []
    for fr in file_reviews:
        if fr.coverage_pct is None:
            continue
        pct_str = f"{fr.coverage_pct:.1f}%"
        if fr.coverage_pct < 80.0:
            pct_str = f"**{pct_str}**"
        rows.append(f"| {fr.file_path} | {fr.covered_lines} | {fr.instrumented_changed_lines} | {pct_str} |")
    if not rows:
        return ""
    return "\n".join([header, alignment, *rows])


def _render_file_section(fr: FileReview, base_branch: str = "main") -> list[str]:
    """Render a single file's review section."""
    lines = [f"## `{fr.file_path}`", ""]

    if fr.issues:
        lines.extend(_render_issues_table(fr.issues))
        lines.extend(_render_rule_descriptions(fr.issues))

    if fr.duplications:
        lines.extend(_render_duplications(fr.duplications))

    if fr.resolved_duplications:
        lines.extend(_render_resolved_duplications(fr.resolved_duplications, base_branch))

    if not fr.issues and not fr.duplications and not fr.resolved_duplications:
        lines.extend(["No issues.", ""])

    return lines


def _write_markdown(result: ReviewResult, path: Path) -> None:
    """Write a human-readable markdown report."""
    lines: list[str] = [
        f"# Review: {result.project_key} ({result.branch})",
        f"Base: `{result.base_branch}`",
        "",
        (
            f"**Total issues: {result.total_issues}** | "
            f"**Duplication findings: {result.total_duplications}** | "
            f"**Files checked: {result.files_analyzed or len(result.files)}**"
        ),
        "",
    ]

    for fr in result.files:
        lines.extend(_render_file_section(fr, result.base_branch))

    files_with_coverage = [fr for fr in result.files if fr.coverage_pct is not None]
    if files_with_coverage:
        lines.append("## Coverage on changed lines")
        lines.append("")
        lines.append(format_coverage_table(files_with_coverage))
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
