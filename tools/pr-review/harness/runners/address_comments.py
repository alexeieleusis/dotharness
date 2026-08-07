import json
import logging
from pathlib import Path

from harness.backend import Backend
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.runners.common import (
    PR_COMMENTS_SCRIPT_PATH,
    TIMEOUT_GH,
    TIMEOUT_GIT,
    build_subprocess_env,
    fetch_pr_comments,
    find_last_reply_if_marked,
    get_current_user,
    get_gh_token,
    get_head_sha,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    is_draft_pr,
    reply_has_reaction_from,
    run_cmd,
)

logger = logging.getLogger(__name__)

GRAPHQL_QUERY = """
query($owner:String!,$repo:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$cursor){
        pageInfo{endCursor hasNextPage}
        nodes{isResolved}
      }
    }
  }
}
""".strip()

COMMENT_BODY_LABEL = "Comment body:"

GRAPHQL_QUERY_THREAD_IDS = """
query($owner:String!,$repo:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$number){
      reviewThreads(first:100,after:$cursor){
        pageInfo{endCursor hasNextPage}
        nodes{
          isResolved
          comments(first:100){nodes{databaseId}}
        }
      }
    }
  }
}
""".strip()


def run(config: HarnessConfig) -> None:
    with acquire_lock(config.repo_slug):
        _run_locked(config)


def _run_locked(config: HarnessConfig) -> None:
    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)

    prs = _list_prs_to_check(config.repo.name, env)
    script_path = PR_COMMENTS_SCRIPT_PATH
    knowledge_file = config.harness.knowledge_dir / "pr-review" / "address-comment.md"
    instructions_template = knowledge_file.read_text(encoding="utf-8")
    backend = Backend(
        config.harness.backend,
        config.harness.backend_timeout_seconds,
        config.harness.path_prepend,
        {**config.harness.env, "GITHUB_TOKEN": gh_token},
    )

    wdir = str(config.repo.working_dir)
    opencode_dir = str(config.repo.opencode_dir) if config.repo.opencode_dir else None
    plugin_prefix = (
        str(config.repo.opencode_dir.relative_to(config.repo.working_dir)) if config.repo.opencode_dir else None
    )
    original_sha = git_detach_and_record(wdir, env)
    our_login = get_current_user(env)

    logger.info("Found %d open PR(s) to check", len(prs))
    for pr in prs:
        number = pr["number"]
        branch = pr["headRefName"]
        logger.debug("PR #%d (branch=%s): checking for pending feedback", number, branch)
        if is_draft_pr(pr):
            logger.info("PR #%d: draft PR, skipping", number)
            continue
        if not _has_pending_feedback(number, config.repo.name, env):
            logger.info("PR #%d: no pending feedback, skipping", number)
            continue
        _process_single_pr(
            number,
            branch,
            script_path,
            instructions_template,
            backend,
            our_login,
            wdir,
            original_sha,
            config.repo.name,
            env,
            config.address_comments.require_reaction_for_focused_review,
            opencode_dir,
            plugin_prefix,
        )


def _process_single_pr(
    number: int,
    branch: str,
    script_path: Path,
    instructions_template: str,
    backend: Backend,
    our_login: str | None,
    wdir: str,
    original_sha: str,
    repo: str,
    env: dict,
    require_reaction_for_focused_review: bool,
    opencode_dir: str | None = None,
    plugin_prefix: str | None = None,
) -> None:
    logger.info("PR #%d (branch=%s): has pending feedback, processing", number, branch)
    try:
        git_fetch_and_checkout(branch, wdir, env)
        comments = fetch_pr_comments(number, script_path, wdir, env)
        if not comments:
            logger.info("PR #%d: no actionable comments found", number)
            return
        comments = _filter_comments(
            comments, number, repo, our_login, env, require_reaction_for_focused_review, plugin_prefix
        )
        if not comments:
            logger.info("PR #%d: no unresolved comments remain after filtering", number)
            return
        logger.info("PR #%d: found %d comment(s) to address", number, len(comments))
        head_sha = get_head_sha(wdir, env)
        for comment in comments:
            head_sha = _address_single_comment(
                comment, number, instructions_template, backend, wdir, repo, env, head_sha, opencode_dir
            )
            if not _push_branch(number, branch, wdir, env):
                logger.warning(
                    "PR #%d: skipping remaining comments this run after comment %s's push failed",
                    number,
                    comment.get("id", "?"),
                )
                break
    except Exception:
        logger.exception("PR #%d: error", number)
    finally:
        git_restore(original_sha, branch, wdir, env)


def _split_gated_focused_review_comments(comments: list[dict], enabled: bool) -> tuple[list[dict], list[dict]]:
    """Split comments into (gated, ungated). Gated comments are inline threads whose most
    recent reply carries a focused-review-bot marker — meaning it hasn't been addressed
    yet. Once anything is posted after that marker reply (our own completion reply, or a
    further human comment), the thread is no longer gated. Empty when `enabled` is False."""
    if not enabled:
        return [], comments
    gated, ungated = [], []
    for c in comments:
        if c.get("type") == "inline" and find_last_reply_if_marked(c) is not None:
            gated.append(c)
        else:
            ungated.append(c)
    return gated, ungated


def _focused_review_approved(comment: dict, repo: str, our_login: str | None, env: dict) -> bool:
    """True if our_login left a +1 reaction on this comment's (last, marker-carrying) reply."""
    if not our_login:
        return False
    last_reply = find_last_reply_if_marked(comment)
    if last_reply is None or "id" not in last_reply:
        return False
    return reply_has_reaction_from(last_reply["id"], repo, our_login, env)


def _is_comment_already_replied(comment: dict, our_login: str) -> bool:
    """Return True if `our_login` has already replied to this comment."""
    if comment["type"] == "inline":
        replies = comment.get("replies")
        if replies and replies[-1].get("author") == our_login:
            return True
    return comment["type"] == "issue" and comment.get("author") == our_login


def _process_gated_comments(
    gated: list[dict],
    comments: list[dict],
    pr_number: int,
    repo: str,
    our_login: str | None,
    env: dict,
) -> list[dict]:
    """Filter gated comments by approval status, log pending count, and append approved to comments."""
    approved = [c for c in gated if _focused_review_approved(c, repo, our_login, env)]
    pending = len(gated) - len(approved)
    if pending:
        logger.info(
            "PR #%d: %d focused-review comment(s) awaiting a \U0001f44d reaction before addressing",
            pr_number,
            pending,
        )
    return comments + approved


def _filter_comments(
    comments: list[dict],
    pr_number: int,
    repo: str,
    our_login: str | None,
    env: dict,
    require_reaction_for_focused_review: bool = False,
    plugin_prefix: str | None = None,
) -> list[dict]:
    unresolved_ids = _get_unresolved_comment_ids(pr_number, repo, env)
    if unresolved_ids is not None:
        before = len(comments)
        comments = [c for c in comments if c["type"] != "inline" or c.get("id") in unresolved_ids]
        if len(comments) < before:
            logger.info(
                "PR #%d: filtered to %d unresolved comment(s) (was %d)",
                pr_number,
                len(comments),
                before,
            )

    gated, comments = _split_gated_focused_review_comments(comments, require_reaction_for_focused_review)

    if our_login:
        before = len(comments)
        comments = [c for c in comments if not _is_comment_already_replied(c, our_login)]
        if len(comments) < before:
            logger.info(
                "PR #%d: skipped %d already-replied comment(s)",
                pr_number,
                before - len(comments),
            )

    if gated:
        comments = _process_gated_comments(gated, comments, pr_number, repo, our_login, env)

    if plugin_prefix:
        before = len(comments)
        comments = [c for c in comments if c["type"] != "inline" or c.get("path", "").startswith(plugin_prefix + "/")]
        if len(comments) < before:
            logger.info(
                "PR #%d: filtered to %d comment(s) in subdir '%s' (was %d)",
                pr_number,
                len(comments),
                plugin_prefix,
                before,
            )

    return comments


def _is_ancestor(candidate_sha: str, descendant_sha: str, wdir: str, env: dict) -> bool:
    result = run_cmd(
        ["git", "merge-base", "--is-ancestor", candidate_sha, descendant_sha],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    return result.returncode == 0


def _address_single_comment(
    comment: dict,
    pr_number: int,
    instructions_template: str,
    backend: Backend,
    wdir: str,
    repo: str,
    env: dict,
    pre_sha: str,
    opencode_dir: str | None = None,
) -> str:
    """Run the backend for `comment`, then return the resulting HEAD sha (or `pre_sha`
    unchanged if HEAD didn't move / couldn't be read) so the caller can pass it as the
    next comment's `pre_sha` without re-deriving it."""
    cid = comment.get("id", "?")
    ctype = comment.get("type", "?")
    author = comment.get("author", "?")
    logger.info(
        "PR #%d: processing comment (id=%s type=%s author=%s)",
        pr_number,
        cid,
        ctype,
        author,
    )
    comment_instructions = _build_comment_instructions(instructions_template, comment, pr_number, repo)
    post_sha = pre_sha
    try:
        backend.run(comment_instructions, cwd=wdir, opencode_dir=opencode_dir)
        logger.info("PR #%d: comment %s — backend finished", pr_number, cid)
    except Exception:
        logger.exception("PR #%d comment %s: error", pr_number, cid)
    finally:
        post_sha = get_head_sha(wdir, env) or pre_sha
        if pre_sha and post_sha != pre_sha and not _is_ancestor(pre_sha, post_sha, wdir, env):
            logger.error(
                "PR #%d comment %s: HEAD %s..%s is not a fast-forward — branch history was "
                "rewritten during this comment's backend run; earlier commit(s) may have just been discarded",
                pr_number,
                cid,
                pre_sha,
                post_sha,
            )
    return post_sha


def _push_branch(number: int, branch: str, wdir: str, env: dict) -> bool:
    """Push `branch` to origin. Called after every comment (not once at the end of the
    PR) so each addressed comment is safely on GitHub before the next comment's backend
    session starts — bounding the damage if a later session rewrites local history."""
    log_result = run_cmd(
        ["git", "log", "--oneline", f"origin/{branch}..HEAD"],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if log_result.returncode == 0:
        commits = log_result.stdout.decode("utf-8", errors="replace").strip()
        logger.info(
            "PR #%d: about to push branch %s; commits ahead of origin:\n%s",
            number,
            branch,
            commits or "(none)",
        )
    push_result = run_cmd(
        ["git", "push", "origin", branch],
        cwd=wdir,
        env=env,
        timeout=TIMEOUT_GIT,
        check=False,
    )
    if push_result.returncode == 0:
        logger.info("PR #%d: pushed branch %s", number, branch)
        return True
    logger.warning(
        "PR #%d: git push failed (rc=%d): %s",
        number,
        push_result.returncode,
        push_result.stderr.decode("utf-8", errors="replace")[:300],
    )
    return False


def _build_comment_instructions(template: str, comment: dict, pr_number: int, repo: str) -> str:
    ctype = comment["type"]
    lines: list[str] = [f"Type: {ctype}"]

    if ctype == "inline":
        lines += [
            f"File: {comment['path']}:{comment['line']}",
            f"Comment ID: {comment['id']}",
            f"Author: @{comment['author']}",
            f"URL: {comment['url']}",
            "",
            COMMENT_BODY_LABEL,
            comment["body"],
        ]
        if comment.get("diff_hunk"):
            lines += ["", "Diff context:", comment["diff_hunk"]]
        if comment.get("replies"):
            lines.append("")
            lines.append("Thread replies:")
            for r in comment["replies"]:
                lines.append(f"  @{r['author']}: {r['body']}")
    elif ctype == "review":
        lines += [
            f"Comment ID: {comment['id']}",
            f"Author: @{comment['author']}",
            f"State: {comment.get('state', '')}",
            f"URL: {comment.get('url', '')}",
            "",
            COMMENT_BODY_LABEL,
            comment["body"],
        ]
    else:  # issue
        lines += [
            f"Comment ID: {comment['id']}",
            f"Author: @{comment['author']}",
            f"URL: {comment.get('url', '')}",
            "",
            COMMENT_BODY_LABEL,
            comment["body"],
        ]

    detail = "\n".join(lines)
    return f"{template}\n\n---\n\n## Comment to address\n\n{detail}\n\nPR number: {pr_number}\nRepo: {repo}\n"


def _list_prs_to_check(repo: str, env: dict) -> list[dict]:
    """PRs to process: ones authored by us, plus ones assigned to us (e.g. as a fix-it assignee
    on someone else's PR) — deduplicated by PR number."""
    by_number = {pr["number"]: pr for pr in _list_prs(repo, env, "--author") + _list_prs(repo, env, "--assignee")}
    return sorted(by_number.values(), key=lambda p: p["number"])


def _list_prs(repo: str, env: dict, filter_flag: str) -> list[dict]:
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
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return []
    return json.loads(result.stdout)


def _get_unresolved_comment_ids(pr_number: int, repo: str, env: dict) -> set[int] | None:
    """Return REST API databaseIds of inline comments in unresolved threads.
    Returns None if the GraphQL query fails (caller should skip filtering)."""
    owner, repo_name = repo.split("/")
    ids: set[int] = set()
    cursor: str | None = None

    while True:
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={GRAPHQL_QUERY_THREAD_IDS}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo_name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            cmd += ["-f", f"cursor={cursor}"]
        result = run_cmd(cmd, cwd="/", env=env, timeout=TIMEOUT_GH, check=False)
        if result.returncode != 0:
            logger.warning("PR #%d: could not fetch unresolved thread IDs (rc=%d)", pr_number, result.returncode)
            return None
        try:
            data = json.loads(result.stdout)
        except ValueError:
            logger.warning("PR #%d: could not parse GraphQL response for thread IDs", pr_number)
            return None
        threads_page = data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {})
        for t in threads_page.get("nodes", []):
            if not t["isResolved"]:
                for c in t.get("comments", {}).get("nodes", []):
                    db_id = c.get("databaseId")
                    if db_id:
                        ids.add(db_id)
        page_info = threads_page.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    logger.debug("PR #%d: %d unresolved inline comment id(s)", pr_number, len(ids))
    return ids


def _has_pending_feedback(pr_number: int, repo: str, env: dict) -> bool:
    owner, repo_name = repo.split("/")
    cursor: str | None = None
    total_threads = 0
    total_unresolved = 0
    gql_ok = True

    while True:
        cmd = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={GRAPHQL_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"repo={repo_name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            cmd += ["-f", f"cursor={cursor}"]
        gql_result = run_cmd(cmd, cwd="/", env=env, timeout=TIMEOUT_GH, check=False)
        if gql_result.returncode != 0:
            logger.debug("PR #%d: GraphQL review threads query failed (rc=%d)", pr_number, gql_result.returncode)
            gql_ok = False
            break
        data = json.loads(gql_result.stdout)
        threads_page = data.get("data", {}).get("repository", {}).get("pullRequest", {}).get("reviewThreads", {})
        nodes = threads_page.get("nodes", [])
        total_threads += len(nodes)
        total_unresolved += sum(1 for t in nodes if not t["isResolved"])
        page_info = threads_page.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    if gql_ok:
        logger.debug("PR #%d: %d review thread(s), %d unresolved", pr_number, total_threads, total_unresolved)
        if total_unresolved:
            return True

    comments_result = run_cmd(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if comments_result.returncode == 0:
        comments = json.loads(comments_result.stdout)
        bot_logins = {"github-actions[bot]", "dependabot[bot]"}
        human_comments = [c for c in comments if c.get("user", {}).get("login") not in bot_logins]
        logger.debug("PR #%d: %d issue comment(s), %d from humans", pr_number, len(comments), len(human_comments))
        if human_comments:
            return True
    else:
        logger.debug("PR #%d: issue comments query failed (rc=%d)", pr_number, comments_result.returncode)

    return False
