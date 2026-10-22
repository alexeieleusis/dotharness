"""Change context and prompt assembly for the review passes and the address phase."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from harness.runners.local_review.findings import AddressFinding
from harness.runners.local_review.git import git
from harness.runners.local_review.types import LocalReviewTarget

MAX_DESCRIPTION_COMMITS = 50
MAX_DESCRIPTION_CHARS = 20_000
TRUNCATED_MARKER = "(truncated)"
OUTPUT_FILE_PLACEHOLDER = "{OUTPUT_FILE}"
FILE_TEMPLATE = "local-review-file.md"
SUMMARY_TEMPLATE = "local-review-summary.md"
DESIGN_TEMPLATE = "local-review-design.md"
REVIEW_TEMPLATES = (FILE_TEMPLATE, SUMMARY_TEMPLATE, DESIGN_TEMPLATE)
ADDRESS_TEMPLATE = "local-address-finding.md"
ADDRESS_HEADING = "**Finding to address**"
MAX_DIFF_CHARS = 40_000


def build_change_description(working_dir: Path, target: LocalReviewTarget) -> str:
    """`## Change description`: branch, base ref, merge base, then the commit log.

    The log keeps the newest MAX_DESCRIPTION_COMMITS commits (listed oldest first) and the
    whole section is capped at MAX_DESCRIPTION_CHARS characters (cut at a line boundary).
    When either cap applies the text ends with a `(truncated)` line and stays within both caps.
    """
    header = (
        f"## Change description\nBranch: {target.branch}\nBase ref: {target.base_ref}\n"
        f"Merge base: {target.base_sha}\n\n"
    )
    count = git(working_dir, "rev-list", "--count", f"{target.base_sha}..HEAD")
    total = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
    truncated = total > MAX_DESCRIPTION_COMMITS
    # --max-count keeps the newest N commits; --reverse then lists those oldest first.
    proc = git(
        working_dir,
        "log",
        "--reverse",
        "--no-color",
        "--format=%h %s%n%b",
        f"--max-count={MAX_DESCRIPTION_COMMITS}",
        f"{target.base_sha}..HEAD",
    )
    log = proc.stdout.rstrip("\n") if proc.returncode == 0 else ""
    suffix = f"\n{TRUNCATED_MARKER}"
    budget = MAX_DESCRIPTION_CHARS - len(header) - len(suffix)
    if len(log) > budget:
        truncated = True
        cut = log[: max(budget, 0)]
        nl = cut.rfind("\n")
        log = cut[:nl] if nl > 0 else cut
    return header + log + (suffix if truncated else "")


def build_change_context(config_repo_name: str, working_dir: Path, target: LocalReviewTarget, description: str) -> str:
    """Replaces the PR URL / number / description block of the self-review prompts."""
    repo = config_repo_name or working_dir.name
    return f"\n\nRepo: {repo}\nCommit: {target.head_sha}\n\n{description}"


def _with_output_path(instructions: str, output_file: Path) -> str:
    return instructions.replace(OUTPUT_FILE_PLACEHOLDER, str(output_file.resolve()))


def _guide(extra_knowledge: str | None) -> str:
    return f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else ""


def _static(vibe_heal_context: str | None) -> str:
    return f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else ""


def build_file_prompt(
    instructions: str,
    output_file: Path,
    file_section: str,
    change_context: str,
    extra_knowledge: str | None = None,
    vibe_heal_context: str | None = None,
) -> str:
    """Per-file pass. `file_section` is `common.build_file_review_section(...)` output."""
    return (
        _with_output_path(instructions, output_file)
        + _guide(extra_knowledge)
        + file_section
        + change_context
        + _static(vibe_heal_context)
    )


def build_summary_prompt(
    instructions: str,
    output_file: Path,
    files: Sequence[str],
    change_context: str,
    extra_knowledge: str | None = None,
    vibe_heal_context: str | None = None,
) -> str:
    return (
        _with_output_path(instructions, output_file)
        + _guide(extra_knowledge)
        + change_context
        + "\n\nFiles reviewed:\n"
        + "\n".join(files)
        + _static(vibe_heal_context)
    )


def build_design_prompt(
    instructions: str,
    output_file: Path,
    diff_sections: str,
    change_context: str,
    extra_knowledge: str | None = None,
    vibe_heal_context: str | None = None,
) -> str:
    """Design pass. `diff_sections` is every changed file's `build_file_review_section` concatenated."""
    return (
        _with_output_path(instructions, output_file)
        + _guide(extra_knowledge)
        + diff_sections
        + change_context
        + _static(vibe_heal_context)
    )


def build_address_prompt(
    instructions: str,
    finding: AddressFinding,
    resolution_path: Path,
    review_head_sha: str,
    diff: str,
    vibe_heal_context: str | None = None,
) -> str:
    """Template text, then the finding under `**Finding to address**` (the template has no placeholders)."""
    diff_text = diff if len(diff) <= MAX_DIFF_CHARS else diff[:MAX_DIFF_CHARS] + f"\n{TRUNCATED_MARKER}\n"
    parts = [
        instructions.rstrip(),
        "",
        "---",
        "",
        ADDRESS_HEADING,
        "",
        f"- finding id: {finding.id}",
        f"- review head sha (12 chars): {review_head_sha[:12]}",
        f"- file: {finding.file or '(none given)'}",
        f"- line: {finding.line}",
        f"- resolution file (absolute path, write it in Step 4): {resolution_path}",
        "",
        "Finding block:",
        "",
        finding.block.text.rstrip(),
        "",
        "Diff of the file against the merge base:",
        "",
        "```diff",
        diff_text.rstrip(),
        "```",
    ]
    if vibe_heal_context:
        parts += ["", "Static-analysis (vibe-heal) context:", "", vibe_heal_context.strip()]
    return "\n".join(parts) + "\n"
