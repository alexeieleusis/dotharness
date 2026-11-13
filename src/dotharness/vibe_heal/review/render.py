"""Render a review result as GitHub-flavoured Markdown: the body, the inline comments and the fallback body.

The code host posts what this module renders. It enforces only its own size limits, anchor rules and fallback.
"""

from dotharness.code_host import Anchor, InlineComment
from dotharness.vibe_heal.review.models import FileReview, ReviewIssue, ReviewResult
from dotharness.vibe_heal.review.reporter import format_coverage_table

MAX_OVERFLOW_BODY_LINES = 100


def _issue_body(issue: ReviewIssue) -> str:
    body = f"**{issue.rule}** {issue.message}"
    if issue.doc_url:
        body += f"\n\n{issue.doc_url}"
    if issue.root_cause:
        body += (
            f"\n\n<details>\n\n<summary>{issue.rule} — why this matters</summary>\n\n{issue.root_cause}\n\n</details>"
        )
    return body


def _collect_file_comments(
    file_review: FileReview, base_branch: str, comments: list[tuple[str, int, str]], nearby_lines: list[str]
) -> None:
    for issue in file_review.issues:
        body = _issue_body(issue)
        if issue.on_changed_line:
            comments.append((file_review.file_path, issue.line, body))
        else:
            nearby_lines.append(f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}")
    for dup in file_review.duplications:
        locations = ", ".join(f"`{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in dup.other_locations)
        body = (
            f"**Duplication detected** (lines {dup.from_line}-{dup.to_line})\n\n"
            f"This block is duplicated in: {locations}"
        )
        # anchor_line is a changed line in the diff, so the host accepts the comment. Older reports lack it.
        anchor = dup.anchor_line if dup.anchor_line is not None else dup.from_line
        comments.append((file_review.file_path, anchor, body))
    for res in file_review.resolved_duplications:
        other = "\n".join(f"- `{loc.file_path}` lines {loc.from_line}-{loc.to_line}" for loc in res.other_locations)
        body = (
            f"**Possible missed update** - lines {res.main_from_line}-{res.main_to_line} in `{base_branch}` were duplicated.\n\n"
            "You modified this region. The duplication may be resolved here, but check the other instances:\n\n"
            f"{other}"
        )
        comments.append((file_review.file_path, res.anchor_new_line, body))


def _build_summary(report: ReviewResult, inline: int, capped: int, nearby: int) -> str:
    label_suffix = f" [{report.project_label}]" if report.project_label else ""
    sha_suffix = f" (commit {report.head_sha[:7]})" if report.head_sha else ""
    total = inline + capped + nearby
    if not total:
        return f"SonarQube{label_suffix}: no findings on or near changed lines.{sha_suffix}"
    counts = [(inline, "inline"), (capped, "capped"), (nearby, "near changed lines")]
    breakdown = ", ".join(f"{n} {name}" for n, name in counts if n)
    return f"SonarQube{label_suffix}: {total} finding(s) ({breakdown}).{sha_suffix}"


def _build_overflow_section(overflow: list[tuple[str, int, str]], max_inline: int) -> str:
    visible = overflow[:MAX_OVERFLOW_BODY_LINES]
    overflow_lines = [f"- **{path}:{line}** {body.splitlines()[0]}" for path, line, body in visible]
    extra = len(overflow) - len(visible)
    suffix = f"\n_...and {extra} more not shown._" if extra else ""
    return (
        f"**{len(overflow)} finding(s) over the {max_inline}-comment inline limit**"
        f" (capped to avoid GitHub rate limits):\n" + "\n".join(overflow_lines) + suffix
    )


def build_review(report: ReviewResult, max_inline: int) -> tuple[str, list[InlineComment]]:
    """Review body and at most ``max_inline`` inline comments for ``report``. The host adds its own caps."""
    all_comments: list[tuple[str, int, str]] = []
    nearby_lines: list[str] = []
    for file_review in report.files:
        _collect_file_comments(file_review, report.base_branch, all_comments, nearby_lines)

    comments, overflow = all_comments[:max_inline], all_comments[max_inline:]

    body_parts = [_build_summary(report, len(comments), len(overflow), len(nearby_lines))]
    if nearby_lines:
        body_parts.append(
            "**Issues near changed lines** (outside diff — shown here instead of inline):\n" + "\n".join(nearby_lines)
        )
    if overflow:
        body_parts.append(_build_overflow_section(overflow, max_inline))
    files_with_coverage = [fr for fr in report.files if fr.coverage_pct is not None]
    if files_with_coverage:
        body_parts.append("**Coverage on changed lines:**\n\n" + format_coverage_table(files_with_coverage))
    return "\n\n".join(body_parts), [InlineComment(body, Anchor(path, line, "RIGHT")) for path, line, body in comments]


def build_fallback_body(report: ReviewResult) -> str:
    """Top-level fallback text: every issue, duplication and resolved duplication as a bullet, plus coverage."""
    sha_suffix = f" (commit {report.head_sha[:7]})" if report.head_sha else ""
    lines: list[str] = [f"SonarQube fallback review{sha_suffix}"] if sha_suffix else []
    seen_rules: set[str] = set()
    for file_review in report.files:
        for issue in file_review.issues:
            lines.append(f"- **{issue.rule}** ({file_review.file_path}:{issue.line}) {issue.message}")
            if issue.root_cause and issue.rule not in seen_rules:
                seen_rules.add(issue.rule)
                lines.append(
                    f"\n<details>\n\n<summary>{issue.rule} — why this matters</summary>"
                    f"\n\n{issue.root_cause}\n\n</details>\n"
                )
        for dup in file_review.duplications:
            lines.append(
                f"- **Duplication** ({file_review.file_path} lines {dup.from_line}-{dup.to_line}) "
                f"duplicated in {len(dup.other_locations)} other location(s)"
            )
        for res in file_review.resolved_duplications:
            lines.append(
                f"- **Possible missed update** ({file_review.file_path}) - "
                f"lines {res.main_from_line}-{res.main_to_line} in main were duplicated; "
                f"{len(res.other_locations)} other instance(s) may need updating"
            )
    files_with_coverage = [fr for fr in report.files if fr.coverage_pct is not None]
    if files_with_coverage:
        lines.extend(["", "**Coverage on changed lines:**\n\n" + format_coverage_table(files_with_coverage)])
    return "\n".join(lines)
