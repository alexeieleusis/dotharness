import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass

from harness.config import HarnessConfig
from harness.runners.common import (
    REGRET_REVIEW_MARKER,
    author_matches,
    count_diff_lines,
    get_changed_files,
    get_pr_base_branch,
    run_cmd,
)

logger = logging.getLogger(__name__)

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


# A unified-diff hunk header. git omits a side's line count when it is 1, so each count
# is optional and defaults to 1 (e.g. `@@ -5 +5,2 @@` means old line 5 only, new lines
# 5-6).
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# The opening tokens of a `git blame` default-format line: an optional `^` (git marks
# the file's boundary — first — commit with it) followed by the commit SHA.
_BLAME_SHA_RE = re.compile(r"^(\^?)([0-9a-f]{4,40})\s")


@dataclass
class IntroducingHunk:
    """One surviving (file, line_range, introducing_sha, introducing_pr_number)
    combination from find_introducing_prs (regret-review-requirements.md §7.1 G6): a
    contiguous removed/modified range in the current PR's diff, the commit git blame
    attributes the range's pre-fix content to, and one of the PRs GitHub reports as
    containing that commit. Carries everything the comment-matching leaf
    (requirements.md §7.2) needs: introducing_pr_number — which PR to fetch comments
    from (deduplicated across the run by the caller); path plus old_start/old_end
    (inclusive pre-fix line numbers in the current PR's diff) — the blamed change;
    introducing_sha — the commit to diff against its own parent to find the hunk window
    a fetched comment must fall within; new_start/new_end (inclusive) — the same
    hunk's post-image in the current PR's diff (a pure deletion has an empty new
    range, where new_end < new_start)."""

    path: str
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    introducing_sha: str
    introducing_pr_number: int


def _remaining_budget(budget_start: float, budget_seconds: int) -> int | None:
    """Whole seconds left in the pass's shared wall-clock budget, or None once it is
    exhausted. The budget is config.regret_review.regret_review_timeout: one overall cap
    on this pass's per-PR git/gh calls (regret-review-requirements.md §7.5/§8), checked
    and decremented before every call rather than spent as fresh per-call constants —
    the remaining seconds are what the next call's run_cmd timeout gets, so every call
    in the sequence shares one budget instead of each getting a full one."""
    remaining = int(budget_seconds - (time.monotonic() - budget_start))
    return remaining if remaining > 0 else None


def _run_within_budget(
    cmd: list[str],
    wdir: str,
    env: dict,
    pr_number: int,
    what: str,
    budget_start: float,
    budget_seconds: int,
) -> subprocess.CompletedProcess | None:
    """run_cmd guarded by the pass's shared budget: the call's timeout is whatever
    seconds remain in it, and None is returned — so the caller can stop the sequence
    and fail open — when the budget is exhausted before the call starts or the call
    itself runs out the remaining time (subprocess.TimeoutExpired). A normal non-zero
    exit is NOT budget exhaustion: it is returned as usual for the caller's own
    fail-open handling."""
    remaining = _remaining_budget(budget_start, budget_seconds)
    if remaining is None:
        logger.debug(
            "PR #%d: regret-review budget (%ds) exhausted before %s — stopping the sequence",
            pr_number,
            budget_seconds,
            what,
        )
        return None
    try:
        return run_cmd(cmd, cwd=wdir, env=env, timeout=remaining, check=False)
    except subprocess.TimeoutExpired:
        logger.debug(
            "PR #%d: regret-review budget (%ds) exhausted during %s — stopping the sequence",
            pr_number,
            budget_seconds,
            what,
        )
        return None


def _removed_modified_ranges(diff: str) -> list[tuple[int, int, int, int]]:
    """Each contiguous removed/modified line range in a --unified=0 diff, as
    (old_start, old_count, new_start, new_count) read from its `@@ -a,b +c,d @@`
    header. Hunks with old count 0 (b == 0) are pure additions and produce no range —
    there is no pre-fix content to blame. With --unified=0 each surviving hunk is
    exactly one contiguous changed region (no context lines), so the ranges need no
    merging."""
    ranges: list[tuple[int, int, int, int]] = []
    for line in diff.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or 1)
        new_start = int(match.group(3))
        new_count = int(match.group(4) or 1)
        if old_count > 0:
            ranges.append((old_start, old_count, new_start, new_count))
    return ranges


def _blamed_shas(blame_output: str) -> list[str]:
    """The commit SHA of each line a `git blame -L` default-format output attributes,
    in blame order. The `^` prefix git adds to a file's boundary commit is stripped;
    lines not matching the expected shape carry no usable attribution and are
    skipped."""
    shas: list[str] = []
    for line in blame_output.splitlines():
        match = _BLAME_SHA_RE.match(line)
        if match is not None:
            shas.append(match.group(2))
    return shas


def _pr_gate_setup(pr: dict, config: HarnessConfig, wdir: str, env: dict) -> tuple[int, str, list[str]] | None:
    """The G2 author gate plus the gate setup: (number, base_branch, changed files), or
    None when this PR is skipped entirely — no number, an author that does not match
    config.regret_review.authors (an empty login never matches: author_matches does a
    substring check against string-form allowlists, where the empty string would
    vacuously match, so an authorless PR is treated as non-matching, fail-closed), or an
    undeterminable base branch. Checked before any git/gh work, so a non-matching PR
    does zero subprocess calls."""
    number = pr.get("number")
    if number is None:
        return None
    login = (pr.get("author") or {}).get("login", "")
    if not login or not author_matches(login, config.regret_review.authors):
        logger.debug("PR #%d: author %r does not match regret_review.authors — skipping regret review", number, login)
        return None
    base_branch = get_pr_base_branch(number, config.repo.name, env)
    if not base_branch:
        logger.debug("PR #%d: could not determine the base branch — skipping regret review", number)
        return None
    return number, base_branch, get_changed_files(base_branch, wdir, env)


def _diff_files_within_gate(
    files: list[str],
    base_branch: str,
    wdir: str,
    env: dict,
    number: int,
    max_diff_lines: int,
    budget_start: float,
    budget_seconds: int,
) -> dict[str, str] | None:
    """G1 + G3 input in one pass: one `git diff --unified=0 origin/{base}...HEAD` per
    changed file. Each diff's +/- lines feed the bugfix-shaped gate's total
    (count_diff_lines, the same rule build_file_review_section uses), and the gate exits
    as soon as the running total exceeds max_diff_lines — the remaining files are never
    diffed, so the caller makes no blame or API call. The diff strings are returned for
    the hunk parse, so each file is diffed exactly once. None means stop: the gate
    tripped or the budget was exhausted."""
    diffs: dict[str, str] = {}
    total_diff_lines = 0
    for file in files:
        result = _run_within_budget(
            ["git", "diff", f"origin/{base_branch}...HEAD", "--unified=0", "--", file],
            wdir,
            env,
            number,
            f"git diff {file}",
            budget_start,
            budget_seconds,
        )
        if result is None:
            return None
        if result.returncode != 0:
            logger.debug(
                "PR #%d: git diff failed for %s (exit %d) — treating the file as unchanged",
                number,
                file,
                result.returncode,
            )
            continue
        diff = result.stdout.decode("utf-8")
        diffs[file] = diff
        total_diff_lines += count_diff_lines(diff)
        if total_diff_lines > max_diff_lines:
            logger.debug(
                "PR #%d: %d changed lines exceed regret_review.max_diff_lines=%d — not bugfix-shaped, skipping",
                number,
                total_diff_lines,
                max_diff_lines,
            )
            return None
    return diffs


def _blame_removed_ranges(
    diffs: dict[str, str],
    blame_ref: str,
    wdir: str,
    env: dict,
    number: int,
    budget_start: float,
    budget_seconds: int,
) -> list[tuple[str, int, int, int, int, str]] | None:
    """G3: blame each removed/modified range against the pre-fix state. For each hunk
    with pre-image lines (pure additions have nothing to blame) it runs `git blame -L
    <start>,<end> <blame_ref> -- <path>`, producing one (file, old_start, old_end,
    new_start, new_end, sha) tuple per blamed range/commit combination: a range can span
    several commits, while a range whose lines all blame to the same commit is one
    candidate, not one per line (§7.1 step 3). A range whose blame fails (out of
    bounds, a rename the blame lost) is skipped. None means the budget was exhausted
    mid-sequence."""
    blamed: list[tuple[str, int, int, int, int, str]] = []
    for file, diff in diffs.items():
        for old_start, old_count, new_start, new_count in _removed_modified_ranges(diff):
            old_end = old_start + old_count - 1
            new_end = new_start + new_count - 1
            result = _run_within_budget(
                ["git", "blame", "-L", f"{old_start},{old_end}", blame_ref, "--", file],
                wdir,
                env,
                number,
                f"git blame {file}:{old_start}-{old_end}",
                budget_start,
                budget_seconds,
            )
            if result is None:
                return None
            if result.returncode != 0:
                logger.debug(
                    "PR #%d: git blame failed for %s:%d-%d (exit %d) — skipping the range",
                    number,
                    file,
                    old_start,
                    old_end,
                    result.returncode,
                )
                continue
            for sha in dict.fromkeys(_blamed_shas(result.stdout.decode("utf-8", errors="replace"))):
                blamed.append((file, old_start, old_end, new_start, new_end, sha))
    return blamed


def _resolve_commit_prs(
    shas: list[str],
    repo: str,
    env: dict,
    number: int,
    budget_start: float,
    budget_seconds: int,
) -> dict[str, list[int]] | None:
    """G5: resolve each unique introducing SHA (already deduplicated per G4) to its PR
    via `gh api repos/{repo}/commits/{sha}/pulls`. A zero-PR result — an API failure or
    a confirmed no-PR answer (direct push, deleted PR) — omits that SHA from the result:
    its lines are skipped silently (debug only, no retry, fail open, §7.1 step 4).
    Multiple PR results (e.g. a cherry-pick) are all kept, so every one is evaluated
    independently downstream. None means the budget was exhausted mid-sequence."""
    prs_by_sha: dict[str, list[int]] = {}
    for sha in shas:
        result = _run_within_budget(
            ["gh", "api", f"repos/{repo}/commits/{sha}/pulls"],
            "/",
            env,
            number,
            f"gh api commits/{sha}/pulls",
            budget_start,
            budget_seconds,
        )
        if result is None:
            return None
        if result.returncode != 0:
            logger.debug(
                "PR #%d: commit %s PR lookup failed (exit %d) — skipping its lines",
                number,
                sha,
                result.returncode,
            )
            continue
        try:
            pulls = json.loads(result.stdout)
        except ValueError:
            logger.debug("PR #%d: commit %s PR lookup returned malformed JSON — skipping its lines", number, sha)
            continue
        prs_by_sha[sha] = [p["number"] for p in pulls if isinstance(p, dict) and "number" in p]
    return prs_by_sha


def find_introducing_prs(pr: dict, config: HarnessConfig, wdir: str, env: dict) -> list[IntroducingHunk]:
    """The blame → introducing-commit → introducing-PR resolver
    (regret-review-requirements.md §7.1): given the current PR, the list of surviving
    (file, line_range, introducing_sha, introducing_pr_number) combinations whose
    introducing PR's comments the comment-matching leaf (§7.2) should check — or []
    whenever the pass is gated off, nothing resolves, or the budget is exhausted. All
    of those are expected, fail-open outcomes: no error is logged above debug level and
    no partial finding is produced (§7.1 step 4, §8).

    Gated before any blame or API work (see _pr_gate_setup / _diff_files_within_gate):
    the PR's author must match config.regret_review.authors (G2, via author_matches),
    and the PR must be bugfix-shaped — total added + removed lines across every
    changed file at or below config.regret_review.max_diff_lines (G1).

    Each hunk of each file's `git diff --unified=0 origin/{base}...HEAD` with pre-image
    lines (old count > 0; pure additions have nothing to blame) has its old-side range
    blamed with `git blame -L <start>,<end> <ref> -- <path>` against the pre-fix state:
    the merge-base of origin/{base} and HEAD, the commit the diff's old side is
    measured against (the ... comparison diffs against the merge-base), so the diff's
    old-side line numbers and the blamed positions line up exactly — never the fix's
    own commit. If the merge-base cannot be resolved, origin/{base} is blamed instead.

    The blamed SHAs are deduplicated within the run (G4) and each unique one is
    resolved to its PR(s) via the GitHub commit→PR API (G5); multiple PR results are
    all kept.

    The whole per-PR sequence of git/gh calls (diffs, merge-base, blames, commit→PR
    lookups) shares one wall-clock budget — config.regret_review.regret_review_timeout,
    checked and decremented via _run_within_budget before every call, each call's
    timeout being the remaining seconds. An exhausted budget stops the sequence and
    fails open at []. The two shared plumbing helpers used for the gate setup
    (get_pr_base_branch, get_changed_files) keep their own fixed timeouts, as they do
    for every other pass; no per-call timeout constant is added for this pass."""
    setup = _pr_gate_setup(pr, config, wdir, env)
    if setup is None:
        return []
    number, base_branch, files = setup
    if not files:
        return []

    budget_start = time.monotonic()
    budget_seconds = config.regret_review.regret_review_timeout

    diffs = _diff_files_within_gate(
        files,
        base_branch,
        wdir,
        env,
        number,
        config.regret_review.max_diff_lines,
        budget_start,
        budget_seconds,
    )
    if diffs is None:
        return []

    merge_base = _run_within_budget(
        ["git", "merge-base", f"origin/{base_branch}", "HEAD"],
        wdir,
        env,
        number,
        "git merge-base",
        budget_start,
        budget_seconds,
    )
    if merge_base is None:
        return []
    if merge_base.returncode == 0:
        blame_ref = merge_base.stdout.decode("utf-8").strip()
    else:
        logger.debug("PR #%d: merge-base resolution failed — blaming origin/%s instead", number, base_branch)
        blame_ref = f"origin/{base_branch}"

    blamed = _blame_removed_ranges(diffs, blame_ref, wdir, env, number, budget_start, budget_seconds)
    if blamed is None:
        return []

    # G4: deduplicate the SHAs within the run — a commit blamed from several ranges or
    # files gets exactly one commit→PR lookup.
    prs_by_sha = _resolve_commit_prs(
        sorted({sha for *_, sha in blamed}), config.repo.name, env, number, budget_start, budget_seconds
    )
    if prs_by_sha is None:
        return []

    # G6: one IntroducingHunk per surviving (range, commit, PR) combination — a range
    # that blames to two commits, or a commit that merged via two PRs, yields one
    # instance per combination.
    hunks: list[IntroducingHunk] = []
    for file, old_start, old_end, new_start, new_end, sha in blamed:
        for pr_number in prs_by_sha.get(sha, []):
            hunks.append(
                IntroducingHunk(
                    path=file,
                    old_start=old_start,
                    old_end=old_end,
                    new_start=new_start,
                    new_end=new_end,
                    introducing_sha=sha,
                    introducing_pr_number=pr_number,
                )
            )
    logger.debug("PR #%d: regret review found %d introducing hunk(s)", number, len(hunks))
    return hunks
