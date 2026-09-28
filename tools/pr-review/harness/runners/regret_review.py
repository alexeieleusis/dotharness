import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass

from harness.backend import Backend
from harness.config import HarnessConfig
from harness.runners.common import (
    PR_COMMENTS_SCRIPT_PATH,
    REGRET_REVIEW_MARKER,
    TIMEOUT_GH,
    _build_pr_metadata_trailer,
    author_matches,
    count_diff_lines,
    fetch_pr_comments,
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
    `# Previously flagged review comments` heading, one section per finding under it (a `###` heading naming
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
    return "# Previously flagged review comments\n\n" + "\n\n".join(sections) + f"\n\n{REGRET_REVIEW_MARKER}"


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


@dataclass
class RegretCandidate:
    """One surviving (comment, blamed_change) pair from find_regret_candidates
    (regret-review-requirements.md §7.2 steps 1-2, G4): a comment fetched from an
    introducing PR that is of a location-carrying type, sits on the blamed file, and
    falls within a diff hunk the introducing commit's own change occupied in that
    file, paired with the exact IntroducingHunk (the blamed change) it matched. This
    is the intermediate shape the two later leaves build from:

    - The backend-judgment leaf (§7.2 steps 3-4) batches the whole candidate list
      into one prompt, which per candidate gives the original comment's body
      (comment_body), its author (comment_author), and the diff of the change the
      comment was left on (comment_diff_hunk — the unified-diff snippet from the
      introducing PR's own diff that GitHub anchors the comment to; "" when the
      comment's diff_hunk is empty).
    - The poster leaf (§7.6) builds one RegretFinding per confirmed candidate:
      comment_id and hunk.introducing_pr_number name the original comment,
      comment_url is the direct URL to it, and hunk.path plus the blamed range
      (hunk.old_start/hunk.old_end pre-fix, hunk.new_start/hunk.new_end post-image)
      is the current PR's file/region — RegretFinding.line collapses it to one line:
      hunk.new_start when the post-image is non-empty, hunk.old_start for a pure
      deletion that has no post-image line."""

    comment_id: int | str
    comment_body: str
    comment_author: str
    comment_diff_hunk: str
    comment_url: str
    hunk: IntroducingHunk


def _budget_clock(budget_start: float | None) -> float:
    """The shared budget's start time: the caller-supplied one, so find_introducing_prs
    and find_regret_candidates can be threaded onto the same clock instead of each
    resetting it (regret-review-requirements.md §7.5/§8), or a fresh time.monotonic()
    when a caller (e.g. a direct unit test) has none to share."""
    return budget_start if budget_start is not None else time.monotonic()


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


def _hunk_line_ranges(diff: str) -> list[tuple[int, int, int, int]]:
    """Each hunk of a --unified=0 diff as (old_start, old_end, new_start, new_end)
    inclusive line ranges read from its `@@ -a,b +c,d @@` header — the same header
    parse as _removed_modified_ranges, which keeps the counts, shaped here into the
    ranges find_regret_candidates' hunk-tolerant match (requirements.md §7.2 step 2,
    glossary "Diff hunk") compares a fetched comment's line against. An empty side —
    count 0, a pure addition's old side or a pure deletion's new side — ends one line
    before it starts (end < start), so no line can fall within it."""
    ranges: list[tuple[int, int, int, int]] = []
    for line in diff.splitlines():
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2) or 1)
        new_start = int(match.group(3))
        new_count = int(match.group(4) or 1)
        ranges.append((old_start, old_start + old_count - 1, new_start, new_start + new_count - 1))
    return ranges


def _line_within_hunk_ranges(line: int, ranges: list[tuple[int, int, int, int]]) -> bool:
    """Whether a comment's line falls within any hunk window (requirements.md §7.2
    step 2): the line may sit on either side of the hunk — the new side, for a
    comment on the code as the introducing commit wrote it, or the old side, for a
    comment on a line that change deleted. An empty side cannot match: its end is
    below its start, so the comparison is simply false there."""
    for old_start, old_end, new_start, new_end in ranges:
        if old_start <= line <= old_end or new_start <= line <= new_end:
            return True
    return False


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


def find_introducing_prs(
    pr: dict, config: HarnessConfig, wdir: str, env: dict, budget_start: float | None = None
) -> list[IntroducingHunk]:
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
    for every other pass; no per-call timeout constant is added for this pass.

    budget_start defaults to a fresh clock (time.monotonic()) when omitted, but the
    caller is expected to pass the same start time on to find_regret_candidates — the
    two functions' calls are one bugfix-shaped sequence sharing a single
    regret_review_timeout cap, not two independent ones (regret-review-requirements.md
    §7.5/§8)."""
    setup = _pr_gate_setup(pr, config, wdir, env)
    if setup is None:
        return []
    number, base_branch, files = setup
    if not files:
        return []

    budget_start = _budget_clock(budget_start)
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


def find_regret_candidates(
    introducing_hunks: list[IntroducingHunk],
    config: HarnessConfig,
    wdir: str,
    env: dict,
    budget_start: float | None = None,
) -> list[RegretCandidate]:
    """The comment-fetch + hunk-match leaf (regret-review-requirements.md §7.2 steps 1-2):
    given find_introducing_prs' output, the candidate (comment, blamed_change) pairs
    whose original comments the backend-judgment leaf (§7.2 steps 3-4) should judge —
    or [] whenever there is nothing to judge, which is a valid, expected outcome, not
    an error (§7.2 step 4): empty input, no surviving location-carrying comment, no
    hunk match, or a budget exhaustion. This module logs nothing above debug level and
    produces no partial candidate set: a budget exhaustion stops the sequence and
    fails open at [], the same way find_introducing_prs does.

    G1: one fetch_pr_comments call per unique introducing PR number across the whole
    run (the numbers are deduplicated before the fetch; each PR's comments are then
    matched locally against every hunk that resolved to it), pointed at the
    introducing PR's number through the existing shared helper — the pass adds no new
    fetch mechanism. A fetch that fails or comes back empty simply leaves that PR
    without candidates: its hunks match against nothing, and the other PRs proceed.
    The fetch itself keeps fetch_pr_comments' own fixed cap; the shared budget gates
    whether the fetch starts and caps every hunk-diff call that follows.

    G2: only "inline"-typed comments survive the type filter — the only type that can
    carry a path/line; "review"-typed comments (from the PR's /reviews endpoint, which
    carry no path/line) and "issue"-typed comments are never candidates.

    G3: the hunk window of a (commit, file) pair is computed by diffing the
    introducing commit against its own parent, restricted to the blamed file —
    `git diff {sha}^ {sha} --unified=0 -- {path}`, one small run_cmd call per unique
    (commit, file) pair, shared by every hunk that blames to that commit in that
    file. Each hunk's @@ line ranges (both sides) are the window: a comment is a
    candidate when its path equals the blamed file and its line falls within any of
    that commit's hunk windows for the file — the same localized region of change,
    not only the exact blamed line and not the whole file (requirements.md §7.2
    step 2, glossary "Diff hunk", §11 item 2). A commit whose diff fails (e.g. a
    root commit whose parent does not exist) drops its candidates, fail open.

    G4: the returned list holds one RegretCandidate per surviving (comment,
    IntroducingHunk) combination: a comment that falls within the window of two
    distinct blamed ranges yields one candidate per range, the full pair set the
    next leaf batches into one prompt.

    The whole per-PR sequence of git/gh calls (the comment fetches and the hunk-diff
    calls) shares one wall-clock budget — config.regret_review.regret_review_timeout,
    checked and decremented via _run_within_budget / _remaining_budget the same way
    find_introducing_prs does; an exhausted budget stops the sequence and fails open
    at [].

    budget_start defaults to a fresh clock when omitted, but the caller is expected to
    pass find_introducing_prs' own budget_start through here instead: the two calls are
    one bugfix-shaped sequence sharing a single regret_review_timeout cap, not two
    independent ones (regret-review-requirements.md §7.5/§8) — otherwise a PR could
    spend the full timeout in find_introducing_prs and another full timeout here."""
    if not introducing_hunks:
        return []
    budget_start = _budget_clock(budget_start)
    budget_seconds = config.regret_review.regret_review_timeout

    comments_by_pr = _fetch_comments_by_pr(introducing_hunks, wdir, env, budget_start, budget_seconds)
    if comments_by_pr is None:
        return []

    windows_by_commit_file = _introducing_hunk_windows(introducing_hunks, wdir, env, budget_start, budget_seconds)
    if windows_by_commit_file is None:
        return []

    candidates = _match_candidates(introducing_hunks, comments_by_pr, windows_by_commit_file)
    logger.debug("regret-review: %d candidate(s) from %d introducing hunk(s)", len(candidates), len(introducing_hunks))
    return candidates


def _fetch_comments_by_pr(
    introducing_hunks: list[IntroducingHunk],
    wdir: str,
    env: dict,
    budget_start: float,
    budget_seconds: int,
) -> dict[int, list[dict]] | None:
    """G1: one fetch_pr_comments call per unique introducing PR number across the whole
    run, pointed at the introducing PR's number through the existing shared helper. A
    fetch that fails or comes back empty simply leaves that PR without candidates: its
    hunks match against nothing, and the other PRs proceed. The fetch itself keeps
    fetch_pr_comments' own fixed cap; the shared budget only gates whether the fetch
    starts. None means the budget was exhausted mid-sequence."""
    comments_by_pr: dict[int, list[dict]] = {}
    for pr_number in sorted({h.introducing_pr_number for h in introducing_hunks}):
        if _remaining_budget(budget_start, budget_seconds) is None:
            logger.debug(
                "PR #%d: regret-review budget (%ds) exhausted before fetching comments — stopping the sequence",
                pr_number,
                budget_seconds,
            )
            return None
        comments_by_pr[pr_number] = fetch_pr_comments(pr_number, PR_COMMENTS_SCRIPT_PATH, wdir, env)
    return comments_by_pr


def _introducing_hunk_windows(
    introducing_hunks: list[IntroducingHunk],
    wdir: str,
    env: dict,
    budget_start: float,
    budget_seconds: int,
) -> dict[tuple[str, str], list[tuple[int, int, int, int]]] | None:
    """G3: one hunk-window diff per unique (commit, file) pair —
    `git diff {sha}^ {sha} --unified=0 -- {path}` — shared by every hunk that blames
    to that commit in that file; each diff's hunk line ranges are the windows a
    fetched comment must fall within. A diff that fails (e.g. a root commit whose
    parent does not exist) drops that commit's candidates, fail open; None means the
    budget was exhausted mid-sequence."""
    prs_by_sha: dict[str, set[int]] = {}
    for hunk in introducing_hunks:
        prs_by_sha.setdefault(hunk.introducing_sha, set()).add(hunk.introducing_pr_number)
    windows: dict[tuple[str, str], list[tuple[int, int, int, int]]] = {}
    for sha, path in sorted({(h.introducing_sha, h.path) for h in introducing_hunks}):
        result = _run_within_budget(
            ["git", "diff", f"{sha}^", sha, "--unified=0", "--", path],
            wdir,
            env,
            min(prs_by_sha[sha]),
            f"git diff {sha}^ {sha} -- {path}",
            budget_start,
            budget_seconds,
        )
        if result is None:
            return None
        if result.returncode != 0:
            logger.debug(
                "regret-review: hunk-window diff failed for %s -- %s (exit %d) — dropping that commit's candidates",
                sha,
                path,
                result.returncode,
            )
            continue
        windows[(sha, path)] = _hunk_line_ranges(result.stdout.decode("utf-8"))
    return windows


def _match_candidates(
    introducing_hunks: list[IntroducingHunk],
    comments_by_pr: dict[int, list[dict]],
    windows_by_commit_file: dict[tuple[str, str], list[tuple[int, int, int, int]]],
) -> list[RegretCandidate]:
    """G2 + G4: one candidate per surviving (comment, IntroducingHunk) combination —
    a comment of a location-carrying type, on the blamed file, within the introducing
    commit's hunk window for that file. Pure — no I/O; every fetch and diff has
    already happened."""
    candidates: list[RegretCandidate] = []
    for hunk in introducing_hunks:
        windows = windows_by_commit_file.get((hunk.introducing_sha, hunk.path), [])
        for comment in comments_by_pr.get(hunk.introducing_pr_number, []):
            if comment.get("type") != "inline":
                continue
            if comment.get("path") != hunk.path:
                continue
            line = comment.get("line")
            if line is None or not _line_within_hunk_ranges(line, windows):
                continue
            candidates.append(
                RegretCandidate(
                    comment_id=comment["id"],
                    comment_body=comment.get("body", ""),
                    comment_author=comment.get("author", ""),
                    comment_diff_hunk=comment.get("diff_hunk", ""),
                    comment_url=comment.get("url", ""),
                    hunk=hunk,
                )
            )
    return candidates


# A verdict line of the backend's structured response to build_regret_judgment_prompt
# (regret-review-requirements.md §7.2 step 3, G2): `CANDIDATE <n>: YES|NO — <one-sentence
# reason>`, where n is the 1-based index the candidate carries in the prompt's
# `## Regret Candidates` section. The trailing rationale must be present — a bare
# `CANDIDATE <n>: YES` with nothing after it does not satisfy the contract — but its
# text is deliberately not captured: RegretFinding (requirements.md §7.6) carries no
# reason, and the verdict alone determines what the poster does.
_JUDGMENT_LINE_RE = re.compile(r"^CANDIDATE\s+(\d+):\s*(YES|NO)\s*[—-]\s*\S.*$")


def _render_candidate(index: int, candidate: RegretCandidate) -> str:
    """One numbered block of build_regret_judgment_prompt's `## Regret Candidates`
    section: the blamed file/region from the current PR's diff, the original
    comment's author and full body, and the diff of the change the comment was
    left on — everything the judgment question (requirements.md G3) needs to be
    answered for this candidate. The 1-based index is the same number the
    output contract's verdict lines refer back to."""
    hunk = candidate.hunk
    if hunk.new_end >= hunk.new_start:
        region = (
            f"{hunk.path} lines {hunk.old_start}-{hunk.old_end} (pre-fix), "
            f"replaced by lines {hunk.new_start}-{hunk.new_end} (post-fix)"
        )
    else:
        region = f"{hunk.path} lines {hunk.old_start}-{hunk.old_end} (pre-fix); deleted by this fix"
    body = candidate.comment_body.strip()
    body_block = ("> " + "\n> ".join(body.splitlines())) if body else "(the comment has no body)"
    diff_hunk = candidate.comment_diff_hunk or "(no diff anchor)"
    return (
        f"### Candidate {index}\n"
        f"Blamed file/region in this PR's diff: {region}\n"
        f"Original comment (from PR #{hunk.introducing_pr_number}, by {candidate.comment_author}):\n"
        f"{body_block}\n"
        f"Diff of the change the comment was left on:\n{diff_hunk}"
    )


def build_regret_judgment_prompt(
    instructions: str,
    extra_knowledge: str | None,
    candidates: list[RegretCandidate],
    diff_sections: str,
    pr: dict,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> str:
    """The prompt for the regret-review pass's single backend invocation
    (regret-review-requirements.md §7.2 step 3): the review-regret.md instructions,
    the optional `## Additional Review Guide` section (the operator's
    review_knowledge_file, spliced in exactly as build_design_review_prompt and
    build_traceability_review_prompt do, so the regret pass honors the same extra
    guidance as the other passes), the current PR's fix diff, a `## Regret Candidates`
    section with one numbered block per candidate from find_regret_candidates, and the
    shared PR-metadata trailer — the same `_build_pr_metadata_trailer` the design-review
    and requirement-traceability prompt builders use. The whole candidate list is
    batched into this one prompt: the wiring leaves make exactly one backend call
    per current-PR run, never one per candidate. `diff_sections` is pre-built by
    the caller with build_file_review_section (common.py), exactly as every other
    pass's prompt is, which keeps this function pure like the other prompt
    builders."""
    candidates_section = "\n\n## Regret Candidates\n" + "\n\n".join(
        _render_candidate(index, candidate) for index, candidate in enumerate(candidates, start=1)
    )
    return (
        instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + diff_sections
        + candidates_section
        + _build_pr_metadata_trailer(pr, pr_number, repo_name, commit_sha, pr_description, vibe_heal_context)
    )


def _collect_verdicts(response: str, candidates: list[RegretCandidate]) -> dict[int, str] | None:
    """Shared verdict-line scanner for parse_regret_judgment and
    is_complete_judgment_response (regret-review-requirements.md §7.2 step 4): walks the
    backend's response, collecting each verdict line (`CANDIDATE <n>: YES|NO — rationale`)
    keyed by its 1-based candidate index, and returns the mapping. Lines that don't match
    the verdict shape, or that fall outside 1..len(candidates), are ignored here — they
    count against completeness, which the caller decides on. Returns None when the response
    is malformed at the line level — the same candidate index carrying more than one
    verdict line — since a double verdict means the response can't be trusted as a whole."""
    verdicts: dict[int, str] = {}
    for raw_line in response.splitlines():
        match = _JUDGMENT_LINE_RE.match(raw_line.strip())
        if match is None:
            continue
        index = int(match.group(1))
        if not 1 <= index <= len(candidates):
            logger.debug(
                "regret-review: verdict line for candidate %d falls outside the judged set (1-%d) — ignoring it",
                index,
                len(candidates),
            )
            continue
        if index in verdicts:
            logger.debug(
                "regret-review: candidate %d has more than one verdict line — the whole response is malformed",
                index,
            )
            return None
        verdicts[index] = match.group(2)
    return verdicts


def parse_regret_judgment(response: str, candidates: list[RegretCandidate]) -> list[RegretFinding]:
    """The response parser next to build_regret_judgment_prompt
    (regret-review-requirements.md §7.2 step 4): one RegretFinding per
    `CANDIDATE <n>: YES` line of the backend's structured response, where n is
    the 1-based index the candidate carries in the prompt and maps to
    candidates[n-1]. A verdict line must carry the full contract shape —
    `CANDIDATE <n>: YES|NO` followed by a dash and a non-empty rationale — a
    bare `CANDIDATE <n>: YES` with no rationale does not count. The response is
    trusted only when every candidate from 1 to len(candidates) has exactly one
    such verdict line: a candidate with no verdict line, or with more than one,
    makes the *whole* response malformed, even if every other candidate parsed
    cleanly — a response that silently omits some candidates must not be
    allowed to post the ones it did cover. Lines outside 1..len(candidates),
    and any line that does not match the verdict shape at all, are ignored
    rather than counted against completeness. Each finding carries the
    candidate's current-PR file/region — hunk.path plus one line:
    hunk.new_start when the fix leaves a post-image line, hunk.old_start for a
    pure deletion that has no post-image line — and the candidate's
    introducing-PR number and the original comment's id/body/author/url.
    Findings are returned in candidate order, one per YES verdict whose
    candidate carries an integer comment_id (only inline comments carry the
    integer id RegretFinding requires). An incomplete or otherwise malformed
    response therefore yields [] — this pass fails toward silence: no retry,
    no escalation, and nothing here changes common.run_pr_level_pass's own
    retry semantics. Callers that must tell this function's two [] outcomes
    apart — "all verdicts present, none said YES" (a confirmed pass) versus
    "the response is unparseable/truncated" (an inconclusive one) — check
    is_complete_judgment_response first; self_review._run_regret_review's
    has_comment_fn does exactly that so it can keep from persisting a
    done-state on an inconclusive check."""
    if not candidates:
        return []
    verdicts = _collect_verdicts(response, candidates)
    if verdicts is None:
        return []
    if len(verdicts) != len(candidates):
        logger.debug(
            "regret-review: response covers %d of %d candidates — the whole response is malformed",
            len(verdicts),
            len(candidates),
        )
        return []

    findings: list[RegretFinding] = []
    for index in range(1, len(candidates) + 1):
        if verdicts[index] != "YES":
            continue
        candidate = candidates[index - 1]
        if not isinstance(candidate.comment_id, int):
            logger.debug(
                "regret-review: candidate %d's comment id %r is not an int — RegretFinding requires the integer "
                "only inline comments carry; ignoring its verdict",
                index,
                candidate.comment_id,
            )
            continue
        hunk = candidate.hunk
        finding_line = hunk.new_start if hunk.new_end >= hunk.new_start else hunk.old_start
        findings.append(
            RegretFinding(
                path=hunk.path,
                line=finding_line,
                introducing_pr_number=hunk.introducing_pr_number,
                comment_id=candidate.comment_id,
                comment_body=candidate.comment_body,
                comment_author=candidate.comment_author,
                comment_url=candidate.comment_url,
            )
        )
    return findings


def is_complete_judgment_response(response: str, candidates: list[RegretCandidate]) -> bool:
    """True when the backend's response is a complete, well-formed judgment: every
    candidate from 1..len(candidates) carries exactly one verdict line, and no candidate is
    double-verified (regret-review-requirements.md §7.2 step 4). This is the predicate that
    breaks parse_regret_judgment's [] ambiguity apart: that function collapses both "all
    verdicts present, none said YES" and "response unparseable/truncated" into the same [],
    so a caller that must tell the two apart (self_review._run_regret_review's
    has_comment_fn, which must not persist a done-state on an inconclusive check, only on a
    confirmed one) checks this first. Vacuously False when candidates is empty, mirroring
    parse_regret_judgment's no-candidates short-circuit."""
    if not candidates:
        return False
    verdicts = _collect_verdicts(response, candidates)
    return verdicts is not None and len(verdicts) == len(candidates)


# Runner-agnostic plumbing shared by the two wiring leaves (self_review.py and
# review_requested.py), kept in this module — not in common.py — because only this
# pass uses it (requirements.md §10: the pass's plumbing lives in its own module,
# with a single owner).


def post_regret_comment(number: int, repo: str, body: str, env: dict) -> bool:
    """Posts the regret-review PR-level comment — the harness posts it, not the backend:
    review-regret.md's output contract is verdict lines only, and the body is built by
    the caller from parse_regret_judgment's findings (regret-review-requirements.md
    §7.6). True if gh accepted it, mirroring common.post_no_linked_ticket_comment's
    shape."""
    result = run_cmd(
        ["gh", "pr", "comment", str(number), "--repo", repo, "--body", body],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "PR #%d: failed to post regret comment: %s",
            number,
            result.stderr.decode("utf-8", errors="replace"),
        )
        return False
    return True


class RecordingBackend:
    """A delegating stand-in for Backend, used by both wiring leaves' _run_regret_review
    (self_review.py and review_requested.py): it records each run's stdout so the
    backend's verdict lines survive common.run_pr_level_pass, which keeps no copy of the
    reply. It deliberately does not subclass Backend: the runner tests replace Backend
    with a MagicMock, and subclassing a MagicMock returns another MagicMock rather than
    a class, which would shadow this wrapper's own run() override. run_pr_level_pass's
    backend: Backend parameter is satisfied at the call site with typing.cast — the
    wrapper delegates every call to the real backend and exposes exactly the same
    run() signature."""

    def __init__(self, wrapped: Backend, holder: dict[str, str]) -> None:
        self._wrapped = wrapped
        self._holder = holder

    def run(
        self,
        instructions: str,
        cwd: str,
        opencode_dir: str | None = None,
        context: str | None = None,
    ) -> subprocess.CompletedProcess:
        result = self._wrapped.run(instructions, cwd=cwd, opencode_dir=opencode_dir, context=context)
        self._holder["reply"] = result.stdout.decode("utf-8", errors="replace")
        return result
