import json
import logging
import os
import re
import signal
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from harness.backend import Backend
from harness.config import SubDir

logger = logging.getLogger(__name__)

TIMEOUT_GH = 30
TIMEOUT_GIT = 60
TIMEOUT_FETCH_COMMENTS = 120
GRACE_PERIOD_SECONDS = 10

PR_COMMENTS_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "pr-comments.py"

FOCUSED_REVIEW_MARKER = "[focused-review-bot]"
INLINE_REVIEW_MARKER = "<!-- osc-review-inline -->"
DESIGN_REVIEW_MARKER = "<!-- osc-review-design -->"
TRACEABILITY_REVIEW_MARKER = "<!-- osc-review-traceability -->"

# Every PR-level pass marker shares the `osc-review` prefix that
# is_review_summary_comment's substring check looks for, so a comment carrying one of
# these (e.g. the traceability pass's terminal "no linked ticket" comment) must be
# excluded there — otherwise it gets mistaken for the file/summary pass's own
# completion marker and short-circuits correctness review.
_PR_LEVEL_PASS_MARKERS = (INLINE_REVIEW_MARKER, DESIGN_REVIEW_MARKER, TRACEABILITY_REVIEW_MARKER)

# requirement-traceability-requirements.md §7.2/§11.2: the early-comment window is a
# hardcoded constant this iteration, not a harness.toml field.
TRACEABILITY_COMMENT_WINDOW_SECONDS = 300

# Matches, in priority order: a full GitHub issue URL, an "owner/repo#N" cross-repo
# shorthand, or a bare "#N" same-repo shorthand (requirement-traceability-requirements.md
# §7.2). The negative lookbehind on the bare form stops it from also matching the "#N"
# tail of an owner/repo#N reference that failed to match as such (it won't, in practice,
# since alternation already consumes that case first) or from matching mid-identifier.
_ISSUE_REF_RE = re.compile(
    r"https://github\.com/(?P<owner1>[\w.-]+)/(?P<repo1>[\w.-]+)/issues/(?P<num1>\d+)"
    r"|(?P<owner2>[\w.-]+)/(?P<repo2>[\w.-]+)#(?P<num2>\d+)"
    r"|(?<![\w/])#(?P<num3>\d+)"
)

# The `linear[bot]` GitHub App's marker for the comment it posts on a PR that closes a
# Linear ticket (requirement-traceability-requirements.md §4/G5 update: a repo that uses
# Linear exclusively has no GitHub Issues at all, so neither _resolve_native_linked_tickets
# nor _ISSUE_REF_RE can ever find anything there — this comment is that repo's only signal).
LINEAR_LINKBACK_MARKER = "<!-- linear-linkback -->"

# Matches the bot comment's `<details><summary><a href="...">TICKET-ID Title</a></summary>`
# opening line, from which the ticket id/URL/title are read directly — no Linear API call.
_LINEAR_LINKBACK_RE = re.compile(
    r'<summary><a href="(?P<url>https://linear\.app/\S+?)">'
    r"(?P<ticket_id>[A-Z][A-Z0-9]*-\d+)\s+(?P<title>[^<]*)</a></summary>"
)


class FatalGitError(Exception):
    pass


def log_called_process_output(e: subprocess.CalledProcessError, log_func: Callable[..., None], label: str) -> None:
    if e.stdout:
        log_func("%s stdout: %s", label, e.stdout.decode("utf-8", errors="replace"))
    if e.stderr:
        log_func("%s stderr: %s", label, e.stderr.decode("utf-8", errors="replace"))


def run_cmd(
    cmd: list[str] | str,
    cwd: str,
    env: dict,
    timeout: int,
    check: bool = True,
    shell: bool = False,
) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(  # noqa: S603
        cmd,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        result = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
        else:
            return result
    except subprocess.TimeoutExpired:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.communicate(timeout=GRACE_PERIOD_SECONDS)
        except subprocess.TimeoutExpired:
            os.killpg(pgid, signal.SIGKILL)
            proc.communicate()
        raise


def git_detach_and_record(cwd: str, env: dict) -> str:
    try:
        run_cmd(["git", "checkout", "--recurse-submodules", "--detach", "HEAD"], cwd=cwd, env=env, timeout=TIMEOUT_GIT)
        result = run_cmd(["git", "rev-parse", "HEAD"], cwd=cwd, env=env, timeout=TIMEOUT_GIT)
        return result.stdout.decode("utf-8").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise FatalGitError(f"git detach failed: {e}") from e  # noqa: TRY003


def git_fetch_and_checkout(branch: str, cwd: str, env: dict) -> None:
    try:
        run_cmd(["git", "fetch", "origin"], cwd=cwd, env=env, timeout=TIMEOUT_GIT)
        run_cmd(
            ["git", "checkout", "--recurse-submodules", "-B", branch, f"origin/{branch}"],
            cwd=cwd,
            env=env,
            timeout=TIMEOUT_GIT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        if e.stdout:
            logger.exception("git checkout stdout: %s", e.stdout.decode("utf-8", errors="replace"))
        if e.stderr:
            logger.exception("git checkout stderr: %s", e.stderr.decode("utf-8", errors="replace"))
        raise FatalGitError(f"git checkout {branch} failed: {e}") from e  # noqa: TRY003


def get_head_sha(wdir: str, env: dict) -> str:
    result = run_cmd(["git", "rev-parse", "HEAD"], cwd=wdir, env=env, timeout=TIMEOUT_GIT, check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


def git_restore(original_sha: str, branch: str, cwd: str, env: dict) -> None:
    try:
        if branch:
            _preserve_unpushed_commits(branch, cwd, env)
        run_cmd(
            ["git", "checkout", "--recurse-submodules", "-f", original_sha],
            cwd=cwd,
            env=env,
            timeout=TIMEOUT_GIT,
            check=False,
        )
        if branch:
            run_cmd(["git", "branch", "-D", branch], cwd=cwd, env=env, timeout=TIMEOUT_GIT, check=False)
    except Exception:
        logger.exception("git restore failed")


def _preserve_unpushed_commits(branch: str, cwd: str, env: dict) -> None:
    """Before `git_restore` force-checks-out elsewhere and deletes the local `branch`,
    save any commit(s) on it that origin/`branch` doesn't have under a recovery ref.

    Normally every commit on `branch` has already been pushed by the time we get here.
    But a backend run can rewrite history mid-run (see address_comments's fast-forward
    check) or a push can simply fail, leaving local-only commits. Deleting the branch in
    that state would silently destroy that work with no trace it ever existed."""
    head_result = run_cmd(["git", "rev-parse", branch], cwd=cwd, env=env, timeout=TIMEOUT_GIT, check=False)
    if head_result.returncode != 0:
        return
    branch_sha = head_result.stdout.decode("utf-8", errors="replace").strip()
    is_pushed = run_cmd(
        ["git", "merge-base", "--is-ancestor", branch_sha, f"origin/{branch}"],
        cwd=cwd,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if is_pushed.returncode == 0:
        return
    recovery_ref = f"refs/harness-recovery/{branch}-{branch_sha[:12]}"
    save_result = run_cmd(
        ["git", "update-ref", recovery_ref, branch_sha], cwd=cwd, env=env, timeout=TIMEOUT_GIT, check=False
    )
    if save_result.returncode == 0:
        logger.warning(
            "git_restore: %s (%s) is not on origin/%s; preserved it at %s before deleting the local branch",
            branch,
            branch_sha,
            branch,
            recovery_ref,
        )
    else:
        logger.error(
            "git_restore: %s has unpushed commit(s) at %s that could NOT be preserved under a recovery ref "
            "— they will be lost when the local branch is deleted",
            branch,
            branch_sha,
        )


def build_subprocess_env(path_prepend: list[str], env_vars: dict[str, str], gh_token: str) -> dict[str, str]:
    env = os.environ.copy()
    if path_prepend:
        env["PATH"] = ":".join(path_prepend) + ":" + env.get("PATH", "")
    env.update(env_vars)
    if gh_token:
        env["GITHUB_TOKEN"] = gh_token
    return env


def get_gh_token(gh_token_cmd: str) -> str:
    result = subprocess.run(gh_token_cmd, shell=True, capture_output=True, text=True, timeout=TIMEOUT_GH)  # noqa: S602
    return result.stdout.strip()


def is_draft_pr(pr: dict) -> bool:
    return bool(pr.get("isDraft"))


def is_pr_open(pr_number: int, repo: str, env: dict) -> bool:
    """Return True if `pr_number` is still open on GitHub right now.

    Fails closed (returns False) if the lookup fails — a long-running batch shouldn't
    post a review to a PR that may have been closed or merged while it was processing.
    """
    result = run_cmd(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "state", "--jq", ".state"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return False
    return result.stdout.decode("utf-8").strip() == "OPEN"


def get_current_user(env: dict) -> str:
    result = run_cmd(["gh", "api", "user", "--jq", ".login"], cwd="/", env=env, timeout=TIMEOUT_GH, check=False)
    return result.stdout.decode("utf-8", errors="replace").strip()


def pr_from_url(url: str, repo: str, env: dict, fields: str) -> dict:
    number = int(url.rstrip("/").split("/")[-1])
    result = run_cmd(
        ["gh", "pr", "view", str(number), "--repo", repo, "--json", fields],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        logger.error("Failed to fetch PR #%d: %s", number, result.stderr.decode())
        return {}
    return json.loads(result.stdout)


def get_requested_reviewers(pr_number: int, repo: str, env: dict) -> list[str]:
    result = run_cmd(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "reviewRequests"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return []
    data = json.loads(result.stdout)
    return [r["login"] for r in data.get("reviewRequests", []) if "login" in r]


def add_reviewer(pr_number: int, repo: str, login: str, env: dict) -> None:
    run_cmd(
        ["gh", "pr", "edit", str(pr_number), "--repo", repo, "--add-reviewer", login],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )


def remove_reviewer(pr_number: int, repo: str, login: str, env: dict) -> None:
    run_cmd(
        ["gh", "pr", "edit", str(pr_number), "--repo", repo, "--remove-reviewer", login],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )


def is_review_summary_comment(body: str) -> bool:
    if any(marker in body for marker in _PR_LEVEL_PASS_MARKERS):
        return False
    lower = body.lower()
    return "review summary" in lower or "osc-review" in lower


def _paginate_gh_comments(path: str, env: dict) -> list[dict] | None:
    """GET-paginate a GitHub REST comments endpoint, returning every comment regardless
    of author (mirrors address_comments.py's _fetch_all_pages pattern). Unlike
    fetch_pr_comments (which goes through scripts/pr-comments.py's cache), this returns
    raw REST dicts that keep the snake_case `created_at` timestamp the cache drops.
    Returns None if any page fails."""
    items: list[dict] = []
    page = 1
    while True:
        result = run_cmd(
            ["gh", "api", "--method", "GET", path, "-F", "per_page=100", "-F", f"page={page}"],
            cwd="/",
            env=env,
            timeout=TIMEOUT_GH,
            check=False,
        )
        if result.returncode != 0:
            return None
        batch = json.loads(result.stdout)
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def _fetch_matching_comments(
    comments_path: str, current_user: str, env: dict, predicate: Callable[[str], bool]
) -> list[dict] | None:
    """Returns the current_user's comments matching predicate (possibly empty), or None
    if the GitHub API call itself failed (e.g. rate limit, transient 5xx) and the result
    is inconclusive."""
    comments = _paginate_gh_comments(comments_path, env)
    if comments is None:
        return None
    return [c for c in comments if c.get("user", {}).get("login") == current_user and predicate(c.get("body", ""))]


def _has_matching_comment(
    comments_path: str, current_user: str, env: dict, predicate: Callable[[str], bool]
) -> bool | None:
    """Returns True/False for a confirmed match/no-match, or None if the GitHub API
    call itself failed (e.g. rate limit, transient 5xx) and the result is inconclusive."""
    matches = _fetch_matching_comments(comments_path, current_user, env, predicate)
    return None if matches is None else bool(matches)


def has_review_summary_comment(pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    return bool(check_review_summary_comment_status(pr_number, repo, current_user, env))


def check_review_summary_comment_status(pr_number: int, repo: str, current_user: str, env: dict) -> bool | None:
    """Tri-state version of has_review_summary_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return _has_matching_comment(
        f"repos/{repo}/issues/{pr_number}/comments", current_user, env, is_review_summary_comment
    )


def is_inline_review_comment(body: str) -> bool:
    return INLINE_REVIEW_MARKER in body


def has_inline_review_comments(pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    return bool(
        _has_matching_comment(f"repos/{repo}/pulls/{pr_number}/comments", current_user, env, is_inline_review_comment)
    )


def has_pr_level_pass_comment(marker: str, pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    """Shared by every PR-level pass (design review, requirement-traceability review,
    ...) whose completion signal is "did I already post an issue-level PR comment
    containing this marker" — extracted per requirement-traceability-requirements.md §7.6,
    closing the reuse pointer design-review-requirements.md §11.5 left here. A PR-level
    pass always posts exactly one PR-level comment per successful run (findings, or a
    "nothing to report" quiet path), so checking issue-level comments alone is enough —
    no need to also check the inline pulls/comments endpoint the way
    has_inline_review_comments does."""
    return bool(check_pr_level_pass_comment_status(marker, pr_number, repo, current_user, env))


def check_pr_level_pass_comment_status(
    marker: str, pr_number: int, repo: str, current_user: str, env: dict
) -> bool | None:
    """Tri-state version of has_pr_level_pass_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return _has_matching_comment(
        f"repos/{repo}/issues/{pr_number}/comments", current_user, env, lambda body: marker in body
    )


def is_design_review_comment(body: str) -> bool:
    return DESIGN_REVIEW_MARKER in body


def has_design_review_comment(pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    """This is the design pass's *only* idempotency signal in review-requested (no
    persisted state there); self-review uses it only as defense-in-depth alongside its
    own persisted design_reviewed_prs state."""
    return has_pr_level_pass_comment(DESIGN_REVIEW_MARKER, pr_number, repo, current_user, env)


def check_design_review_comment_status(pr_number: int, repo: str, current_user: str, env: dict) -> bool | None:
    """Tri-state version of has_design_review_comment: None means the check itself was
    inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return check_pr_level_pass_comment_status(DESIGN_REVIEW_MARKER, pr_number, repo, current_user, env)


def is_traceability_review_comment(body: str) -> bool:
    return TRACEABILITY_REVIEW_MARKER in body


def has_traceability_review_comment(pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    """Mirrors has_design_review_comment exactly (see requirement-traceability-requirements.md
    §7.4/§7.5): review-requested's only idempotency signal for this pass; self-review's
    defense-in-depth check alongside its own persisted traceability_reviewed_prs state.
    Also covers the "no linked ticket found" outcome, which is terminal (§7.3/§11.1) —
    any comment carrying this marker, including that one, counts as done."""
    return has_pr_level_pass_comment(TRACEABILITY_REVIEW_MARKER, pr_number, repo, current_user, env)


def check_traceability_review_comment_status(pr_number: int, repo: str, current_user: str, env: dict) -> bool | None:
    """Tri-state version of has_traceability_review_comment: None means the check itself
    was inconclusive (API failure), as opposed to a confirmed absence of the comment."""
    return check_pr_level_pass_comment_status(TRACEABILITY_REVIEW_MARKER, pr_number, repo, current_user, env)


def get_pr_level_flagged_locations(
    marker: str, pr_number: int, repo: str, current_user: str, env: dict
) -> list[tuple[str, int]]:
    """Shared by every PR-level pass's flagged-locations fetch (design review,
    requirement-traceability review, ...), mirroring has_pr_level_pass_comment's
    extraction (requirement-traceability-requirements.md §7.6). (path, line) pairs
    already flagged by a prior inline comment containing marker on this PR. Checks the
    pulls/comments endpoint directly and is meant to be called on every invocation, not
    just when deciding whether to skip the pass: a pass posts inline findings and the
    closing PR-level comment as separate calls, so a crash in between leaves the
    PR-level marker comment absent while inline comments already exist. Feeding those
    locations back into the prompt lets a retry avoid re-flagging them. Returns an empty
    list on an inconclusive API failure, rather than blocking the run — worst case a
    retry re-flags an already-posted location instead of the whole pass silently never
    running."""
    matches = _fetch_matching_comments(
        f"repos/{repo}/pulls/{pr_number}/comments", current_user, env, lambda body: marker in body
    )
    if not matches:
        return []
    locations = []
    for comment in matches:
        path, line = comment.get("path"), comment.get("line")
        if path and line is not None:
            locations.append((path, line))
    return locations


def get_design_review_flagged_locations(
    pr_number: int, repo: str, current_user: str, env: dict
) -> list[tuple[str, int]]:
    return get_pr_level_flagged_locations(DESIGN_REVIEW_MARKER, pr_number, repo, current_user, env)


def get_traceability_review_flagged_locations(
    pr_number: int, repo: str, current_user: str, env: dict
) -> list[tuple[str, int]]:
    return get_pr_level_flagged_locations(TRACEABILITY_REVIEW_MARKER, pr_number, repo, current_user, env)


def _window_end_iso(pr_created_at: str) -> str | None:
    """pr_created_at + TRACEABILITY_COMMENT_WINDOW_SECONDS, formatted so it string-compares
    correctly against GitHub REST comments' `created_at` (safe the same way
    address_comments.py:441 already relies on for ISO-8601 timestamps). Returns None if
    pr_created_at is missing or unparseable."""
    if not pr_created_at:
        return None
    try:
        created = datetime.fromisoformat(pr_created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    end = created + timedelta(seconds=TRACEABILITY_COMMENT_WINDOW_SECONDS)
    return end.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_early_window_comments(
    pr_number: int, repo: str, pr_created_at: str, env: dict, cache: dict | None = None
) -> list[dict] | None:
    """All issue-timeline comments on pr_number posted at or before the end of the
    early-comment window, regardless of author. Shared by build_early_comment_context and
    resolve_linked_tickets' comment-scan fallback (§7.2). When both are needed for the
    same PR in the same pass invocation, callers pass the same `cache` dict to each call
    so there's exactly one paginated fetch, not two. Returns None on a fetch failure (as
    opposed to a confirmed-empty window), so callers can fail open without confusing "API
    call failed" with "no comments in the window"."""
    if cache is not None and "early_window_comments" in cache:
        return cache["early_window_comments"]
    comments = _paginate_gh_comments(f"repos/{repo}/issues/{pr_number}/comments", env)
    if comments is None:
        result = None
    else:
        window_end = _window_end_iso(pr_created_at)
        result = [] if window_end is None else [c for c in comments if c.get("created_at", "") <= window_end]
    if cache is not None:
        cache["early_window_comments"] = result
    return result


def build_early_comment_context(
    pr_number: int, repo: str, pr_created_at: str, env: dict, cache: dict | None = None
) -> str:
    """Concatenated bodies of every issue-timeline comment (any author, including bots)
    posted within TRACEABILITY_COMMENT_WINDOW_SECONDS of pr_created_at
    (requirement-traceability-requirements.md §7.2). Included in the traceability prompt
    regardless of how the linked ticket was resolved — a clarifying comment posted right
    after opening is useful context even for a natively-linked ticket. Returns "" if there
    are none, or if the fetch itself fails (fails open — a missing early-comment fetch
    degrades the pass's context, not its correctness). Pass the same `cache` dict given to
    resolve_linked_tickets to reuse its comment-scan fallback's fetch instead of repeating
    it."""
    comments = _fetch_early_window_comments(pr_number, repo, pr_created_at, env, cache)
    if not comments:
        return ""
    return "\n\n".join(body for c in comments if (body := c.get("body", "")))


class _IssueFetchError(Exception):
    """Raised by _fetch_issue when a lookup fails inconclusively (rate limit, transient
    5xx, network blip, malformed response) — as opposed to a confirmed absence (see
    _ISSUE_NOT_FOUND_RE) — so callers can distinguish "this number isn't an issue" from
    "we don't know yet" rather than conflating both into None."""


_ISSUE_NOT_FOUND_RE = re.compile(r"could not resolve to an issue", re.IGNORECASE)


def _fetch_issue(number: int, repo: str, env: dict) -> dict | None:
    """title/body for a single GitHub issue, or None for a *confirmed* absence — either
    `number` doesn't exist, or it names a pull request rather than an issue, since `gh
    issue view` itself fails to resolve a PR number (GitHub's GraphQL schema treats
    Issue/PullRequest as distinct types even though they share one number sequence per
    repo). Raises _IssueFetchError for any other failure, since that's inconclusive rather
    than a confirmed absence."""
    result = run_cmd(
        ["gh", "issue", "view", str(number), "--repo", repo, "--json", "number,title,body"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        if _ISSUE_NOT_FOUND_RE.search(stderr):
            return None
        raise _IssueFetchError(stderr)
    try:
        return json.loads(result.stdout)
    except ValueError as e:
        raise _IssueFetchError(str(e)) from e


def _resolve_native_linked_tickets(pr: dict, env: dict) -> list[dict] | None:
    """Primary mechanism (§7.2 step 1): resolve every entry in pr["closingIssuesReferences"]
    via `gh issue view`, using each entry's own repository (a closing reference can point at
    a different repo than the PR's own). Entries that are confirmed absent (see
    _fetch_issue) are dropped. Returns None — rather than the confirmed-empty `[]` — if no
    ticket resolved AND at least one ref failed inconclusively (rate limit, transient 5xx),
    so resolve_linked_tickets can tell "no ticket" apart from "don't know yet" instead of
    treating a transient API hiccup as proof there's no linked ticket."""
    tickets: list[dict] = []
    seen: set[tuple[str, int]] = set()
    inconclusive = False
    for ref in pr.get("closingIssuesReferences") or []:
        number = ref.get("number")
        repository = ref.get("repository") or {}
        owner = (repository.get("owner") or {}).get("login")
        name = repository.get("name")
        if number is None or not owner or not name:
            continue
        ticket_repo = f"{owner}/{name}"
        key = (ticket_repo, number)
        if key in seen:
            continue
        try:
            issue = _fetch_issue(number, ticket_repo, env)
        except _IssueFetchError:
            inconclusive = True
            continue
        if issue is None:
            continue
        seen.add(key)
        tickets.append({
            "number": issue.get("number", number),
            "repo": ticket_repo,
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "source": "closing_keyword",
        })
    if tickets:
        return tickets
    return None if inconclusive else []


def _resolve_match_repo_and_number(match: re.Match, default_repo: str) -> tuple[str, int | None]:
    if match.group("num1"):
        return f"{match.group('owner1')}/{match.group('repo1')}", int(match.group("num1"))
    if match.group("num2"):
        return f"{match.group('owner2')}/{match.group('repo2')}", int(match.group("num2"))
    if match.group("num3"):
        return default_repo, int(match.group("num3"))
    return default_repo, None


def _parse_linear_linkback_comment(body: str) -> dict | None:
    """Parses a `linear[bot]` linkback comment (identified by LINEAR_LINKBACK_MARKER) into
    a ticket dict shaped like the GitHub-issue tickets _resolve_native_linked_tickets/the
    rest of _resolve_comment_linked_tickets produce. No Linear API call is made — the bot
    already embeds the ticket's title and full description directly in the comment body it
    posts to GitHub, which is enough to synthesize the "formal requirement"
    review-traceability.md needs. Returns None if the marker is present but the expected
    `<summary><a href=...>` shape isn't found (unrecognized/changed bot output) — treated
    by the caller the same as "no ticket in this comment", not as an inconclusive lookup,
    since there's no API failure to distinguish from a confirmed absence here."""
    if LINEAR_LINKBACK_MARKER not in body:
        return None
    match = _LINEAR_LINKBACK_RE.search(body)
    if match is None:
        return None
    detail_body = body[match.end() :].split("<!-- linear-review-link -->", 1)[0]
    detail_body = re.sub(r"</?(?:details|p)>", "", detail_body).strip()
    return {
        "number": match.group("ticket_id"),
        "repo": "Linear",
        "title": match.group("title").strip(),
        "body": detail_body,
        "source": "linear_linkback",
    }


def _resolve_comment_issue_refs(body: str, repo: str, env: dict, seen: set[tuple[str, int | str]]) -> list[dict]:
    """Scans a single comment body for GitHub issue references and resolves each via `gh
    issue view`, split out of _resolve_comment_linked_tickets purely to keep that
    function's branching complexity down. A match that 404s, names a pull request rather
    than an issue, or fails to resolve for any other reason (rate limit, transient 5xx) is
    dropped rather than treated as a ticket (see _fetch_issue) — unlike the native
    mechanism, a regex match in free-text comments is already a guess, so an API failure
    here doesn't warrant the same inconclusive-vs-absent distinction resolve_linked_tickets
    makes for closing-keyword references. `seen` is shared and mutated across every
    comment in the scan, so a ticket already found (by this or the Linear-linkback path)
    isn't re-fetched or duplicated."""
    tickets: list[dict] = []
    for match in _ISSUE_REF_RE.finditer(body):
        ticket_repo, number = _resolve_match_repo_and_number(match, repo)
        if number is None:
            continue
        key = (ticket_repo, number)
        if key in seen:
            continue
        try:
            issue = _fetch_issue(number, ticket_repo, env)
        except _IssueFetchError:
            continue
        if issue is None:
            continue
        seen.add(key)
        tickets.append({
            "number": issue.get("number", number),
            "repo": ticket_repo,
            "title": issue.get("title", ""),
            "body": issue.get("body", ""),
            "source": "comment",
        })
    return tickets


def _resolve_comment_linked_tickets(pr: dict, repo: str, env: dict, cache: dict | None = None) -> list[dict]:
    """Fallback mechanism (§7.2 step 2), only ever called when native linking finds
    nothing: scan every early-window comment (any author, including bots — §1 decision 4)
    for a Linear linkback (_parse_linear_linkback_comment) or a GitHub issue reference
    (_resolve_comment_issue_refs). A comment recognized as a Linear linkback is not also
    scanned for GitHub issue references, since it's bot-authored structured output, not
    free text a manual `#N` mention could appear in."""
    pr_number = pr.get("number")
    if pr_number is None:
        return []
    comments = _fetch_early_window_comments(pr_number, repo, pr.get("createdAt", ""), env, cache)
    if not comments:
        return []
    tickets: list[dict] = []
    seen: set[tuple[str, int | str]] = set()
    for comment in comments:
        body = comment.get("body") or ""
        linear_ticket = _parse_linear_linkback_comment(body)
        if linear_ticket is not None:
            key = (linear_ticket["repo"], linear_ticket["number"])
            if key not in seen:
                seen.add(key)
                tickets.append(linear_ticket)
            continue
        tickets.extend(_resolve_comment_issue_refs(body, repo, env, seen))
    return tickets


def resolve_linked_tickets(pr: dict, repo: str, env: dict, cache: dict | None = None) -> list[dict] | None:
    """Resolve the ticket(s) this PR answers (requirement-traceability-requirements.md
    §7.2, §4/G5 update): GitHub's own closing-keyword linking is tried first and, only if
    it finds nothing, an early-comment scan is tried as a fallback — a `linear[bot]`
    linkback comment first, then a GitHub issue reference (see
    _resolve_comment_linked_tickets). `pr` must carry `closingIssuesReferences` and
    `createdAt` (both runners' PR-listing --json field lists are extended to include
    them). Returns a possibly-empty list of {number, repo, title, body, source} dicts,
    `source` being "closing_keyword", "comment", or "linear_linkback" — or None if the
    native mechanism found a closing-keyword reference it couldn't resolve due to an API
    failure rather than a confirmed absence (see
    _resolve_native_linked_tickets). Callers must treat None as "inconclusive, retry next
    run" and never as "no linked ticket" — the comment-scan fallback is skipped in that
    case rather than risked as a second source of false negatives. Pass the same `cache`
    dict to a later build_early_comment_context call for this PR so its comment-scan
    fallback's paginated fetch (if it ran) is reused instead of repeated."""
    tickets = _resolve_native_linked_tickets(pr, env)
    if tickets is None:
        return None
    if tickets:
        return tickets
    return _resolve_comment_linked_tickets(pr, repo, env, cache)


def post_no_linked_ticket_comment(pr_number: int, repo: str, env: dict) -> bool:
    """Posts the §7.3 "no linked ticket found" PR-level comment directly — no backend
    invocation, since there's nothing for a model to judge when resolve_linked_tickets
    found nothing via either mechanism. This outcome is terminal (§7.3/§11.1): once
    posted, has_traceability_review_comment treats this PR as done forever. Returns True
    if the comment was posted successfully."""
    body = (
        "# Requirement Traceability\n"
        "No linked ticket found (no closing-keyword link and no issue reference in the "
        "first 5 minutes of comments) — skipping scope/gap comparison.\n"
        f"{TRACEABILITY_REVIEW_MARKER}"
    )
    result = run_cmd(
        ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "PR #%d: failed to post 'no linked ticket found' comment: %s",
            pr_number,
            result.stderr.decode("utf-8", errors="replace"),
        )
        return False
    return True


def run_pr_level_pass(
    pr_number: int,
    backend: Backend,
    wdir: str,
    is_done: bool,
    build_prompt: Callable[[], str],
    has_comment_fn: Callable[[], bool | None],
    label: str,
    short_circuit: Callable[[], bool | None] | None = None,
) -> bool | None:
    """Shared orchestration skeleton for every PR-level pass (design review, requirement-
    traceability review, ...): noop-if-already-done → optional short-circuit (e.g.
    traceability's "no linked ticket" outcome, which needs no backend invocation) →
    build prompt → run backend → re-verify the completion marker actually landed. Both
    runners' `_run_design_review`/`_run_traceability_review` were structurally identical
    copies of this skeleton (only the instructions file, flagged-locations getter, prompt
    builder, and traceability's ticket short-circuit differed) — extracted here so a 5th
    PR-level pass (dotharness#4) doesn't have to copy it a 5th time.

    `has_comment_fn` may return None for an inconclusive check (API failure) as opposed to
    a confirmed absence — that distinction is preserved in this function's own return value
    (None vs False) rather than collapsed, since self_review.py's callers must not persist
    "done" state on an inconclusive check the same way they must not on a confirmed miss,
    but still want to log the two cases differently. review_requested.py's checkers never
    return None (no persisted state to protect there), so that branch is simply never hit
    for its callers.

    Returns True if the pass is confirmed complete (already done, short-circuited to
    success, or backend ran and the marker was confirmed), False if it's confirmed not
    complete, or None if a post-backend completion check was inconclusive.
    """
    if is_done:
        logger.info("PR #%d: %s already posted, skipping", pr_number, label)
        return True
    if short_circuit is not None:
        outcome = short_circuit()
        if outcome is not None:
            return outcome
    prompt = build_prompt()
    try:
        result = backend.run(prompt, cwd=wdir, context=f"PR #{pr_number} {label}")
        if result.returncode != 0:
            logger.error("PR #%d: %s backend exited %d", pr_number, label, result.returncode)
            return False
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: %s backend timed out", pr_number, label)
        return False
    comment_status = has_comment_fn()
    if comment_status is None:
        logger.warning(
            "PR #%d: could not confirm %s comment status (comment check failed) — "
            "treating as unconfirmed rather than assuming it's missing",
            pr_number,
            label,
        )
        return None
    if not comment_status:
        logger.error(
            "PR #%d: %s backend exited 0 but no %s comment found — treating as failure",
            pr_number,
            label,
            label,
        )
        return False
    return True


def _build_prior_findings_section(
    prior_flagged_locations: list[tuple[str, int]], findings_label: str, comment_label: str
) -> str:
    """Shared by every PR-level pass's prompt builder (design review, requirement-
    traceability review, ...): renders prior_flagged_locations as a "## Already-flagged
    ... findings" section telling the backend not to re-flag them, or "" when empty."""
    if not prior_flagged_locations:
        return ""
    locations = "\n".join(f"- {path}:{line}" for path, line in prior_flagged_locations)
    return (
        f"\n\n## Already-flagged {findings_label} findings\n"
        f"The following file/line locations already have an inline {comment_label} comment "
        f"from a previous attempt on this PR. Do not post a new inline comment for any of "
        f"them:\n{locations}"
    )


def _build_pr_metadata_trailer(
    pr: dict,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> str:
    """Shared by every PR-level pass's prompt builder: the PR URL/number/repo/commit
    line, plus the optional description and static-analysis sections that follow it."""
    return (
        f"\n\nPR URL: {pr.get('url', '')}\nPR number: {pr_number}\n"
        f"Repo: {repo_name}\nCommit: {commit_sha}"
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )


def build_traceability_review_prompt(
    traceability_instructions: str,
    extra_knowledge: str | None,
    diff_sections: str,
    pr: dict,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
    prior_flagged_locations: list[tuple[str, int]],
    tickets: list[dict],
    early_comment_context: str,
) -> str:
    """Shared by self_review.py and review_requested.py, mirroring
    build_design_review_prompt's shape (requirement-traceability-requirements.md §7.1).
    Only ever called once resolve_linked_tickets has already found at least one ticket —
    callers post the "no linked ticket found" comment directly instead of building this
    prompt when there are none (§7.3). `tickets` and `early_comment_context` become the
    `## Linked Ticket(s)`/`## Early PR Comments` sections review-traceability.md expects
    as input; prior_flagged_locations becomes `## Already-flagged scope-creep findings`,
    same shape as build_design_review_prompt's equivalent section."""
    tickets_section = "\n\n## Linked Ticket(s)\n" + "\n\n".join(
        f"### #{t['number']} ({t['repo']}) — source: {t['source']}\n**Title:** {t['title']}\n\n{t['body']}"
        for t in tickets
    )
    early_comments_section = f"\n\n## Early PR Comments\n{early_comment_context}" if early_comment_context else ""
    prior_findings_section = _build_prior_findings_section(
        prior_flagged_locations, "scope-creep", "traceability-review"
    )
    return (
        traceability_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + tickets_section
        + early_comments_section
        + diff_sections
        + prior_findings_section
        + _build_pr_metadata_trailer(pr, pr_number, repo_name, commit_sha, pr_description, vibe_heal_context)
    )


def author_matches(login: str, authors_config: str | list) -> bool:
    if authors_config == "*":
        return True
    return login in authors_config


def list_open_prs_matching_authors(repo: str, authors_config: str | list, cwd: str, env: dict) -> list[dict] | None:
    """List open, non-draft PRs whose author matches authors_config, sorted ascending by number.

    Returns None (rather than []) on fetch failure, so callers can distinguish "gh pr list
    failed" from "confirmed zero open PRs" instead of treating a transient API hiccup as an
    authoritative empty set.
    """
    result = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,headRefName,author,isDraft,baseRefName,headRefOid",
            "--limit",
            "500",
        ],
        cwd=cwd,
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        logger.error("Failed to list PRs: %s", result.stderr.decode("utf-8", errors="replace"))
        return None
    prs = json.loads(result.stdout)
    eligible = [p for p in prs if not is_draft_pr(p) and author_matches(p["author"]["login"], authors_config)]
    return sorted(eligible, key=lambda p: p["number"])


def _list_prs_by_flag(repo: str, cwd: str, env: dict, filter_flag: str) -> list[dict]:
    result = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            filter_flag,
            "@me",
            "--state",
            "open",
            "--json",
            "number,headRefName,isDraft",
            "--limit",
            "500",
        ],
        cwd=cwd,
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout)


def _list_review_requested_prs(repo: str, cwd: str, env: dict) -> list[dict]:
    # gh search prs does not support headRefName/isDraft in --json; fetch numbers first,
    # then hydrate each with the fields we need via gh pr view.
    result = run_cmd(
        [
            "gh",
            "search",
            "prs",
            "user-review-requested:@me",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number",
            "--limit",
            "500",
        ],
        cwd=cwd,
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return []
    stubs = json.loads(result.stdout)
    prs = []
    for stub in stubs:
        detail = run_cmd(
            ["gh", "pr", "view", str(stub["number"]), "--repo", repo, "--json", "number,headRefName,isDraft"],
            cwd=cwd,
            env=env,
            timeout=TIMEOUT_GH,
            check=False,
        )
        if detail.returncode == 0:
            prs.append(json.loads(detail.stdout))
    return prs


def list_open_prs_for_current_user(repo: str, cwd: str, env: dict) -> list[dict]:
    """List open, non-draft PRs where the running user is the author, is assigned, or has a review
    requested from them (union of the three), deduplicated by PR number, sorted ascending."""
    by_number: dict[int, dict] = {}
    for pr in (
        _list_prs_by_flag(repo, cwd, env, "--author")
        + _list_prs_by_flag(repo, cwd, env, "--assignee")
        + _list_review_requested_prs(repo, cwd, env)
    ):
        by_number.setdefault(pr["number"], pr)
    eligible = [p for p in by_number.values() if not is_draft_pr(p)]
    return sorted(eligible, key=lambda p: p["number"])


def get_pr_base_branch(pr_number: int, repo: str, env: dict) -> str:
    result = run_cmd(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "baseRefName", "--jq", ".baseRefName"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


def get_pr_head_sha(pr_number: int, repo: str, env: dict) -> str:
    result = run_cmd(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "headRefOid", "--jq", ".headRefOid"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


def get_pr_description(pr_number: int, repo: str, env: dict) -> str:
    result = run_cmd(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--repo",
            repo,
            "--json",
            "title,body",
            "--jq",
            '"## PR Description\n**Title:** " + .title + "\n\n" + (.body // "")',
        ],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8").strip()


def get_changed_files(base_branch: str, wdir: str, env: dict, expected_sha: str = "") -> list[str]:
    if expected_sha:
        actual = get_head_sha(wdir, env)
        if actual and actual != expected_sha:
            logger.warning("get_changed_files: local HEAD %s differs from expected %s", actual, expected_sha)
    result = run_cmd(
        ["git", "diff", "--name-only", f"origin/{base_branch}...HEAD"],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if result.returncode != 0:
        logger.error(
            "get_changed_files failed (exit %d): %s",
            result.returncode,
            result.stderr.decode("utf-8", errors="replace")[:500],
        )
        return []
    return [f for f in result.stdout.decode("utf-8").splitlines() if f.strip()]


def get_file_diff(file_path: str, base_branch: str, wdir: str, env: dict) -> str:
    result = run_cmd(
        ["git", "diff", f"origin/{base_branch}...HEAD", "--", file_path],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.decode("utf-8")


def build_file_review_section(file: str, diff: str, abs_path: str) -> str:
    """Return the file section for the review prompt.

    If the file is new or the diff covers ≥75% of the file's lines, omit the diff
    and ask the reviewer to look at the full file instead.
    """
    file_ref = f"@{abs_path}" if os.path.exists(abs_path) else ""

    omit = False
    reason = ""

    if file_ref:
        if "--- /dev/null" in diff:
            omit = True
            reason = "This is a new file — the diff is the entire file."
        else:
            diff_lines = sum(
                1 for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
            )
            try:
                with open(abs_path, encoding="utf-8", errors="replace") as fh:
                    total_lines = sum(1 for _ in fh)
            except Exception:
                total_lines = 0
            if total_lines > 0 and diff_lines >= total_lines * 3 / 4:
                omit = True
                pct = diff_lines * 100 // total_lines
                reason = f"The diff touches {diff_lines} of {total_lines} lines ({pct}% of the file)."

    if omit:
        note = f"Note: {reason} The diff has been omitted; please review the whole file instead."
        return f"\n\n{file_ref}\n\n## File: {file}\n{note}"
    return f"\n\n{file_ref}\n\n## Diff for {file}\n{diff}"


def build_design_review_prompt(
    design_instructions: str,
    extra_knowledge: str | None,
    diff_sections: str,
    pr: dict,
    pr_number: int,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
    prior_flagged_locations: list[tuple[str, int]],
) -> str:
    """Shared by self_review.py and review_requested.py so the design pass's prompt
    shape lives in one place rather than being assembled twice.

    prior_flagged_locations (design-review-requirements.md §7.1) is required, not
    optional: callers must fetch it via get_design_review_flagged_locations on every
    invocation, since it's what lets a retry after a partial failure skip re-flagging
    inline findings it already posted."""
    prior_findings_section = _build_prior_findings_section(prior_flagged_locations, "design", "design-review")
    return (
        design_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + diff_sections
        + prior_findings_section
        + _build_pr_metadata_trailer(pr, pr_number, repo_name, commit_sha, pr_description, vibe_heal_context)
    )


def get_vibe_heal_context(subdirs: list[SubDir], working_dir: str, branch: str) -> str:
    if not subdirs:
        return ""
    seen_keys: set[str] = set()
    parts: list[str] = []
    for subdir in subdirs:
        props_path = Path(working_dir) / subdir.path / "sonar-project.properties"
        project_key = _read_sonar_project_key(props_path)
        if not project_key or project_key in seen_keys:
            continue
        seen_keys.add(project_key)
        review_path = Path.home() / ".vibe-heal" / "reviews" / project_key / branch / "review.md"
        cleaned = _read_vibe_heal_review(review_path, project_key)
        if cleaned:
            parts.append(cleaned)
    return "\n\n".join(parts)


def _read_vibe_heal_review(review_path: Path, project_key: str) -> str | None:
    reviews_base = (Path.home() / ".vibe-heal" / "reviews" / project_key).resolve()
    try:
        review_path.resolve().relative_to(reviews_base)
    except ValueError:
        return None
    if not review_path.exists():
        return None
    try:
        cleaned = review_path.read_text(encoding="utf-8")
    except Exception:
        return None
    while True:
        new_cleaned = re.sub(r"<details>.*?</details>", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
        if new_cleaned == cleaned:
            break
        cleaned = new_cleaned
    return re.sub(r"</?details>", "", cleaned, flags=re.IGNORECASE).strip()


def _read_sonar_project_key(props_path: Path) -> str | None:
    if not props_path.exists():
        return None
    try:
        for line in props_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("sonar.projectKey="):
                value = stripped.split("=", 1)[1].strip()
                if not value:
                    return None
                if "/" in value or "\\" in value or ".." in value:
                    return None
                return value
    except Exception:  # noqa: S110
        pass
    return None


def fetch_pr_comments(pr_number: int, script_path: Path, wdir: str, env: dict) -> list[dict]:
    """Run scripts/pr-comments.py fetch for pr_number and return its cached comments.

    Returns a flat list of dicts, each tagged with a "type" key ("inline", "review", or
    "issue"). Bot-authored issue comments are excluded; inline and review comments are not.
    """
    result = run_cmd(
        ["python3", str(script_path), "fetch", "--pr", str(pr_number)],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_FETCH_COMMENTS,
        check=False,
    )
    if result.returncode != 0:
        logger.warning("PR #%d: failed to fetch comments: %s", pr_number, result.stderr)
        return []

    cache_file = Path.home() / ".harness" / "cache" / f"pr-{pr_number}-comments.json"
    if not cache_file.exists():
        logger.warning("PR #%d: comments cache file not found after fetch", pr_number)
        return []

    data = json.loads(cache_file.read_text(encoding="utf-8"))
    comments: list[dict] = []

    for c in data.get("inline_comments", []):
        comments.append({"type": "inline", **c})

    for c in data.get("review_comments", []):
        comments.append({"type": "review", **c})

    for c in data.get("issue_comments", []):
        author = c.get("author", "")
        if not author.endswith("[bot]"):
            comments.append({"type": "issue", **c})

    logger.debug(
        "PR #%d: cache has %d inline / %d review / %d issue comment(s); %d actionable total",
        pr_number,
        len(data.get("inline_comments", [])),
        len(data.get("review_comments", [])),
        len(data.get("issue_comments", [])),
        len(comments),
    )
    return comments


def find_reply_with_marker(comment: dict, marker: str = FOCUSED_REVIEW_MARKER) -> dict | None:
    """Return the first reply on `comment` whose body contains `marker`, or None."""
    for reply in comment.get("replies") or []:
        if marker in reply.get("body", ""):
            return reply
    return None


def find_last_reply_if_marked(comment: dict, marker: str = FOCUSED_REVIEW_MARKER) -> dict | None:
    """Return `comment`'s LAST reply if it carries `marker`, or None.

    Deliberately last-reply-only, unlike `find_reply_with_marker`'s first-match scan: once
    anything is posted after the marker reply (our own completion note, a further human
    comment), the thread is no longer gated — checking the last reply is what lets an
    addressed thread stop re-triggering.
    """
    replies = comment.get("replies") or []
    if replies and marker in replies[-1].get("body", ""):
        return replies[-1]
    return None


def reply_has_reaction_from(comment_id: int, repo: str, login: str, env: dict) -> bool:
    """Return True if `login` left a +1 reaction on inline review comment `comment_id`.

    Fails closed (returns False) if the reactions API call fails or the response
    can't be parsed — a gate should default to blocking under uncertainty.
    """
    result = run_cmd(
        ["gh", "api", f"repos/{repo}/pulls/comments/{comment_id}/reactions"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        reactions = json.loads(result.stdout)
    except ValueError:
        return False
    if not isinstance(reactions, list):
        return False
    return any(r.get("content") == "+1" and r.get("user", {}).get("login") == login for r in reactions)
