from dataclasses import dataclass

from harness.runners.common import REGRET_REVIEW_MARKER

# Cap on the length of build_regret_comment_body's blockquoted excerpt of an original
# comment's body (regret-review-requirements.md §7.6): longer bodies are cut here and
# the truncation is noted inline with the ellipsis appended at the cut point.
_EXCERPT_MAX_CHARS = 300


@dataclass
class RegretFinding:
    """One confirmed regret finding (regret-review-requirements.md §5): the current
    PR's file/line the finding concerns, plus the introducing PR's original review
    comment whose predicted bug the current PR is now fixing. The backend-judgment leaf
    (requirements.md §7.2 steps 3-4) produces these instances; this module only defines
    the shape they must have to be renderable. comment_url is a direct GitHub URL to
    that original comment, constructible from the cached comment's id as
    https://github.com/{repo}/pull/{pr_number}#discussion_r{id} — only the
    discussion-style form is needed, since issue-typed comments never become findings
    (requirements.md §7.2 step 1)."""

    path: str
    line: int
    introducing_pr_number: int
    comment_id: int
    comment_body: str
    comment_author: str
    comment_url: str


def _excerpt(body: str) -> str:
    """A short blockquoted excerpt of an original comment's body: the first
    _EXCERPT_MAX_CHARS characters, with an inline ellipsis appended only when the
    truncation actually applied. Each line carries its own `> ` prefix so a multi-line
    excerpt renders as a single Markdown blockquote."""
    excerpt = body[:_EXCERPT_MAX_CHARS]
    if len(body) > _EXCERPT_MAX_CHARS:
        excerpt += "…"
    return "> " + "\n> ".join(excerpt.splitlines())


def build_regret_comment_body(findings: list[RegretFinding]) -> str:
    """The full body of the regret comment (regret-review-requirements.md §7.6): a single
    `# Regret Review` heading, one section per finding under it (a `###` heading naming
    the file/region, one line linking to the introducing PR (#<number>) and the direct
    URL to the original comment, and a blockquoted short excerpt of the original
    comment's body), ending with REGRET_REVIEW_MARKER. Pure — no I/O, since every fetch
    has already happened by the time findings reach here. The wiring leaves only ever
    call it with at least one confirmed finding (requirements.md §7.2 step 4: an empty
    candidate set short-circuits before any backend call, so an empty list never
    reaches this function; no "nothing to report" variant is needed, see
    requirements.md §9)."""
    sections = [
        f"### {finding.path}:{finding.line}\n"
        f"Introduced in #{finding.introducing_pr_number} — {finding.comment_url}\n"
        f"{_excerpt(finding.comment_body)}"
        for finding in findings
    ]
    return "# Regret Review\n\n" + "\n\n".join(sections) + f"\n\n{REGRET_REVIEW_MARKER}"
