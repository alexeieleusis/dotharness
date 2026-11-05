#!/usr/bin/env python3
"""
pr-comments.py — Automate the code review comment workflow.

Usage:
  python pr-comments.py fetch [--pr NUMBER] [--output FILE]
  python pr-comments.py reply --commit HASH [--pr NUMBER] [--input FILE]
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CACHE_DIR = Path.home() / ".harness" / "cache"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
    stdin_input: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603
        cmd,
        input=stdin_input,
        capture_output=capture,
        text=True,
        check=check,
    )


def gh_json(args: list[str]) -> Any:
    """Run a gh command that returns JSON."""
    result = run(["gh", *args])
    return json.loads(result.stdout)


def parse_paginated(text: str) -> list:
    """Parse `gh api --paginate` output: one JSON array per page, back to back."""
    decoder = json.JSONDecoder()
    items: list = []
    idx = 0
    text = text.strip()
    while idx < len(text):
        page, end = decoder.raw_decode(text, idx)
        if isinstance(page, list):
            items.extend(page)
        else:
            items.append(page)
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return items


def gh_json_paginated(args: list[str]) -> list:
    """Run a paginated gh api command and concatenate all pages."""
    result = run(["gh", *args])
    return parse_paginated(result.stdout)


def get_pr_number() -> int:
    result = run(["gh", "pr", "view", "--json", "number"], check=False)
    if result.returncode != 0:
        print("Error: Could not detect a PR for the current branch.", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)["number"]


def cache_file(pr_number: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"pr-{pr_number}-comments.json"


def replies_file(pr_number: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"pr-{pr_number}-replies.json"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def _build_inline_comments(raw_inline: list[dict]) -> list[dict]:
    top_level = [c for c in raw_inline if not c.get("in_reply_to_id")]
    replies_map: dict[int, list[dict]] = {}
    for c in raw_inline:
        parent = c.get("in_reply_to_id")
        if parent:
            replies_map.setdefault(parent, []).append(c)

    return [
        {
            "id": c["id"],
            "author": c["user"]["login"],
            "path": c["path"],
            "line": c.get("line") or c.get("original_line"),
            "side": c.get("side", "RIGHT"),
            "body": c["body"],
            "diff_hunk": c["diff_hunk"],
            "url": c["html_url"],
            "replies": [
                {"id": r["id"], "author": r["user"]["login"], "body": r["body"]} for r in replies_map.get(c["id"], [])
            ],
        }
        for c in top_level
    ]


def _build_review_comments(raw_reviews: list[dict]) -> list[dict]:
    # Only include review-level comments that have a body and are change requests
    return [
        {
            "id": f"review-{r['id']}",
            "author": r["user"]["login"],
            "state": r["state"],
            "body": r["body"],
            "url": r["html_url"],
        }
        for r in raw_reviews
        if r.get("body", "").strip() and r["state"] == "CHANGES_REQUESTED"
    ]


def _build_issue_comments(raw_issue_comments: list[dict]) -> list[dict]:
    # Issue comments — bots (CodeRabbit, Graphite, etc.) and humans alike
    return [
        {
            "id": f"issue-comment-{c['id']}",
            "author": c["user"]["login"],
            "author_type": c["user"]["type"],
            "body": c["body"],
            "url": c["html_url"],
        }
        for c in raw_issue_comments
        if c.get("body", "").strip()
    ]


def _print_fetch_summary(
    pr_info: dict,
    dest: Path,
    pr_number: int,
    inline_comments: list[dict],
    review_comments: list[dict],
    issue_comments: list[dict],
) -> None:
    print(f"PR #{pr_number}: {pr_info['title']}")
    print(f"URL:    {pr_info['url']}")
    print(f"Saved:  {dest}")
    print()
    total = len(inline_comments) + len(review_comments) + len(issue_comments)
    print(
        f"Found {len(inline_comments)} inline thread(s), {len(review_comments)} review-level comment(s), {len(issue_comments)} issue comment(s) — {total} total\n"
    )

    for i, c in enumerate(inline_comments, 1):
        print(f"=== Inline comment {i} (id={c['id']}) ===")
        print(f"File   : {c['path']}:{c['line']}")
        print(f"Author : @{c['author']}")
        print(f"Comment: {c['body']}")
        for r in c["replies"]:
            print(f"  ↳ @{r['author']}: {r['body']}")
        print()

    for i, c in enumerate(review_comments, 1):
        print(f"=== Review comment {i} (id={c['id']}) ===")
        print(f"Author : @{c['author']}  [{c['state']}]")
        print(f"Comment: {c['body']}")
        print()

    for i, c in enumerate(issue_comments, 1):
        label = "bot" if c["author_type"] == "Bot" else "user"
        print(f"=== Issue comment {i} (id={c['id']}) ===")
        print(f"Author : @{c['author']}  [{label}]")
        print(f"URL    : {c['url']}")
        print(f"Comment: {c['body']}")
        print()


def cmd_fetch(pr_number: int, output_path: str | None) -> None:
    pr_info = gh_json(["pr", "view", str(pr_number), "--json", "title,url,headRefName"])

    # Inline (file-level) comments
    raw_inline: list[dict] = gh_json_paginated(["api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments", "--paginate"])

    # PR-level reviews (may contain top-level review body)
    raw_reviews: list[dict] = gh_json_paginated(["api", f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/reviews", "--paginate"])

    # Issue comments (general PR timeline comments — where bots like CodeRabbit/Graphite post)
    raw_issue_comments: list[dict] = gh_json_paginated([
        "api",
        f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
        "--paginate",
    ])

    inline_comments = _build_inline_comments(raw_inline)
    review_comments = _build_review_comments(raw_reviews)
    issue_comments = _build_issue_comments(raw_issue_comments)

    data = {
        "pr_number": pr_number,
        "pr_title": pr_info["title"],
        "pr_url": pr_info["url"],
        "branch": pr_info["headRefName"],
        "inline_comments": inline_comments,
        "review_comments": review_comments,
        "issue_comments": issue_comments,
    }

    dest = Path(output_path) if output_path else cache_file(pr_number)
    dest.write_text(json.dumps(data, indent=2))

    _print_fetch_summary(pr_info, dest, pr_number, inline_comments, review_comments, issue_comments)


# ---------------------------------------------------------------------------
# reply
# ---------------------------------------------------------------------------


def _post_reply(cmd_path: str, body: str, comment_id: str | int, errors: list[tuple[Any, str]]) -> None:
    """POST a comment body via gh api, printing the outcome and recording failures."""
    result = run(
        ["gh", "api", cmd_path, "-X", "POST", "-F", "body=@-"],
        check=False,
        stdin_input=body,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        print(f"✗  {msg}")
        errors.append((comment_id, msg))
        return
    # Verify the API actually returned a reply object (has an "id" field)
    try:
        resp = json.loads(result.stdout)
    except json.JSONDecodeError:
        print("✓ (non-JSON response)")
        return
    if "id" in resp:
        print("✓")
    else:
        msg = f"unexpected response: {result.stdout[:200]}"
        print(f"✗  {msg}")
        errors.append((comment_id, msg))


def _already_replied(comment: dict, current_user: str) -> bool:
    """Return True if the current user already posted an 'Addressed in' reply."""
    return any(
        r.get("author") == current_user and r.get("body", "").startswith("Addressed in")
        for r in comment.get("replies", [])
    )


def _collect_reviewers(pr_data: dict) -> set[str]:
    reviewers: set[str] = set()
    for rr in pr_data.get("reviewRequests", []):
        login = rr.get("login") or (rr.get("author") or {}).get("login")
        if login:
            reviewers.add(login)
    for rv in pr_data.get("reviews", []):
        login = (rv.get("author") or {}).get("login")
        if login:
            reviewers.add(login)
    # Remove bots
    return {r for r in reviewers if not r.endswith("[bot]")}


def _request_re_review(pr_number: int) -> None:
    """Request re-review only if the PR is not already approved."""
    pr_data = gh_json(["pr", "view", str(pr_number), "--json", "reviewDecision,reviewRequests,reviews"])
    if pr_data.get("reviewDecision") == "APPROVED":
        print("\nPR is already approved — skipping re-review request.")
        return
    print("\nRequesting re-review …", end=" ")
    reviewers = _collect_reviewers(pr_data)
    if not reviewers:
        print("(no reviewers found)")
        return
    result = run(
        ["gh", "pr", "edit", str(pr_number), "--add-reviewer", ",".join(reviewers)],
        check=False,
    )
    if result.returncode == 0:
        print(f"✓  ({', '.join(sorted(reviewers))})")
    else:
        print(f"✗  {result.stderr.strip()}")


def cmd_reply(pr_number: int, commit_hash: str, input_path: str | None) -> None:
    src = Path(input_path) if input_path else cache_file(pr_number)
    if not src.exists():
        print(f"Error: comments file not found: {src}", file=sys.stderr)
        print("Run `fetch` first.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(src.read_text())

    # Load per-comment reply messages if available
    rfile = replies_file(pr_number)
    per_comment_replies: dict[str, str] = json.loads(rfile.read_text()) if rfile.exists() else {}

    # Resolve full commit hash and build URL
    try:
        full_hash = run(["git", "rev-parse", commit_hash]).stdout.strip()
    except subprocess.CalledProcessError:
        print(f"Error: Commit `{commit_hash}` not found — run `git fetch` first.", file=sys.stderr)
        sys.exit(1)
    repo = gh_json(["repo", "view", "--json", "nameWithOwner"])["nameWithOwner"]
    commit_url = f"https://github.com/{repo}/commit/{full_hash}"

    # Current user login — used for idempotency checks
    current_user = gh_json(["api", "user"])["login"]

    def build_body(comment_id: str | int) -> str:
        key = str(comment_id)
        if key in per_comment_replies:
            return f"{per_comment_replies[key]}\n\n_{commit_url}_"
        return f"Addressed in {commit_url}"

    errors: list[tuple[Any, str]] = []

    for c in data["inline_comments"]:
        if _already_replied(c, current_user):
            print(f"Skipping inline comment {c['id']} — already replied.")
            continue
        print(f"Replying to inline comment {c['id']} ({c['path']}:{c['line']}) …", end=" ")
        _post_reply(
            f"repos/{{owner}}/{{repo}}/pulls/{pr_number}/comments/{c['id']}/replies",
            build_body(c["id"]),
            c["id"],
            errors,
        )

    # Issue comments — reply by posting a new issue comment quoting the original URL
    for c in data.get("issue_comments", data.get("bot_issue_comments", [])):
        label = "bot" if c.get("author_type") == "Bot" else "user"
        print(f"Replying to issue comment {c['id']} (@{c['author']}, {label}) …", end=" ")
        _post_reply(
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            f"> {c['url']}\n\n{build_body(c['id'])}",
            c["id"],
            errors,
        )

    _request_re_review(pr_number)

    if errors:
        print(f"\n{len(errors)} reply(ies) failed — see above.")
        sys.exit(1)
    print("\nAll done!")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Automate PR code review comment workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Fetch PR comments and save to cache")
    p_fetch.add_argument("--pr", type=int, metavar="NUMBER", help="PR number (auto-detected if omitted)")
    p_fetch.add_argument("--output", metavar="FILE", help="Override output file path")

    p_reply = sub.add_parser("reply", help="Reply to comments and request re-review")
    p_reply.add_argument("--commit", required=True, metavar="HASH", help="Commit hash that addresses the comments")
    p_reply.add_argument("--pr", type=int, metavar="NUMBER", help="PR number (auto-detected if omitted)")
    p_reply.add_argument("--input", metavar="FILE", help="Override input file path")

    args = parser.parse_args()
    pr_number = args.pr or get_pr_number()

    if args.command == "fetch":
        cmd_fetch(pr_number, args.output)
    elif args.command == "reply":
        cmd_reply(pr_number, args.commit, args.input)


if __name__ == "__main__":
    main()
