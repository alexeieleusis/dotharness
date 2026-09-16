import json
import logging
import os
import re
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

from harness.config import SubDir

logger = logging.getLogger(__name__)

TIMEOUT_GH = 30
TIMEOUT_GIT = 60
TIMEOUT_FETCH_COMMENTS = 120
GRACE_PERIOD_SECONDS = 10

PR_COMMENTS_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "pr-comments.py"

FOCUSED_REVIEW_MARKER = "[focused-review-bot]"
INLINE_REVIEW_MARKER = "<!-- osc-review-inline -->"


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
    lower = body.lower()
    return "review summary" in lower or "osc-review" in lower


def _has_matching_comment(
    comments_path: str, current_user: str, env: dict, predicate: Callable[[str], bool]
) -> bool | None:
    """Returns True/False for a confirmed match/no-match, or None if the GitHub API
    call itself failed (e.g. rate limit, transient 5xx) and the result is inconclusive."""
    page = 1
    while True:
        result = run_cmd(
            ["gh", "api", "--method", "GET", comments_path, "-F", "per_page=100", "-F", f"page={page}"],
            cwd="/",
            env=env,
            timeout=TIMEOUT_GH,
            check=False,
        )
        if result.returncode != 0:
            return None
        comments = json.loads(result.stdout)
        if not comments:
            return False
        if any(c.get("user", {}).get("login") == current_user and predicate(c.get("body", "")) for c in comments):
            return True
        if len(comments) < 100:
            return False
        page += 1


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
