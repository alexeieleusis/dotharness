import json
import logging
import os
import subprocess
from pathlib import Path

from harness.backend import Backend
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.runners.common import (
    TIMEOUT_GH,
    build_file_review_section,
    build_subprocess_env,
    get_changed_files,
    get_current_user,
    get_file_diff,
    get_gh_token,
    get_pr_base_branch,
    get_pr_description,
    get_pr_head_sha,
    get_vibe_heal_context,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    has_inline_review_comments,
    has_review_summary_comment,
    pr_from_url,
    remove_reviewer,
    run_cmd,
)

logger = logging.getLogger(__name__)


def run(config: HarnessConfig, pr_url: str | None = None) -> None:
    with acquire_lock(config.repo_slug):
        _run_locked(config, pr_url)


def _run_locked(config: HarnessConfig, pr_url: str | None) -> None:
    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)
    current_user = get_current_user(env)

    if pr_url:
        prs = [pr_from_url(pr_url, config.repo.name, env, "number,url,headRefName")]
    else:
        prs = [p for p in _get_prs(config.repo.name, env) if p.get("headRefName")]

    knowledge_dir = Path(config.harness.knowledge_dir) / "pr-review"
    extra_knowledge = (
        config.harness.review_knowledge_file.read_text(encoding="utf-8")
        if config.harness.review_knowledge_file and config.harness.review_knowledge_file.exists()
        else None
    )
    backend = Backend(
        config.harness.backend,
        config.harness.backend_timeout_seconds,
        config.harness.path_prepend,
        {**config.harness.env, "GITHUB_TOKEN": env.get("GITHUB_TOKEN", "")},
    )
    wdir = str(config.repo.working_dir)

    for pr in prs:
        if _should_skip_pr(pr, config.repo.name, current_user, env):
            continue
        _process_pr(pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, current_user)


def _get_prs(repo: str, env: dict) -> list[dict]:
    # gh search prs does not support headRefName in --json; fetch numbers/urls first,
    # then hydrate each with headRefName via gh pr view.
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
            "number,url",
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
    stubs = sorted(json.loads(result.stdout), key=lambda p: p["number"])
    prs = []
    for stub in stubs:
        detail = run_cmd(
            ["gh", "pr", "view", str(stub["number"]), "--repo", repo, "--json", "number,url,headRefName"],
            cwd="/",
            env=env,
            timeout=TIMEOUT_GH,
            check=False,
        )
        if detail.returncode == 0:
            prs.append(json.loads(detail.stdout))
    return prs


def _has_user_approved(pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    page = 1
    while True:
        result = run_cmd(
            [
                "gh",
                "api",
                f"repos/{repo}/pulls/{pr_number}/reviews",
                "-F",
                "per_page=100",
                "-F",
                f"page={page}",
            ],
            cwd="/",
            env=env,
            timeout=TIMEOUT_GH,
            check=False,
        )
        if result.returncode != 0:
            return False
        reviews = json.loads(result.stdout)
        if not reviews:
            return False
        if any(r["state"] == "APPROVED" and r["user"]["login"] == current_user for r in reviews):
            return True
        if len(reviews) < 100:
            return False
        page += 1


def _should_skip_pr(pr: dict, repo: str, current_user: str, env: dict) -> bool:
    pr_number = pr["number"]
    if _has_user_approved(pr_number, repo, current_user, env):
        logger.info("PR #%d already approved by self, skipping", pr_number)
        return True
    if has_review_summary_comment(pr_number, repo, current_user, env):
        logger.info("PR #%d already has review summary comment, skipping", pr_number)
        return True
    if has_inline_review_comments(pr_number, repo, current_user, env):
        logger.info("PR #%d has partial inline review comments, skipping", pr_number)
        return True
    return False


def _process_pr(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    current_user: str,
) -> None:
    pr_number = pr["number"]
    original_sha = git_detach_and_record(wdir, env)
    try:
        git_fetch_and_checkout(pr["headRefName"], wdir, env)
        files_ok = _run_file_reviews(pr, config, knowledge_dir, extra_knowledge, backend, wdir, env)
        summary_ok = _run_summary_review(pr, config, knowledge_dir, extra_knowledge, backend, wdir, env)
        if files_ok and summary_ok:
            remove_reviewer(pr_number, config.repo.name, current_user, env)
    except Exception:
        logger.exception("PR #%d: error", pr_number)
    finally:
        git_restore(original_sha, pr["headRefName"], wdir, env)


def _run_file_reviews(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
) -> bool:
    pr_number = pr["number"]
    file_instructions = (knowledge_dir / "review-file.md").read_text(encoding="utf-8")
    vibe_heal_context = get_vibe_heal_context(config.repo.subdirs, wdir, pr["headRefName"])
    pr_description = get_pr_description(pr_number, config.repo.name, env)
    base_branch = get_pr_base_branch(pr_number, config.repo.name, env)
    commit_sha = get_pr_head_sha(pr_number, config.repo.name, env)
    files = get_changed_files(base_branch, wdir, env, expected_sha=commit_sha)

    all_ok = True
    for file in files:
        diff = get_file_diff(file, base_branch, wdir, env)
        abs_path = os.path.join(wdir, file)
        file_section = build_file_review_section(file, diff, abs_path)
        prompt = _build_file_prompt(
            file_instructions,
            extra_knowledge,
            file_section,
            pr,
            config.repo.name,
            commit_sha,
            pr_description,
            vibe_heal_context,
        )
        try:
            backend.run(prompt, cwd=wdir)
        except subprocess.TimeoutExpired:
            logger.exception("PR #%d file %s: backend timed out", pr_number, file)
            all_ok = False
    return all_ok


def _build_file_prompt(
    file_instructions: str,
    extra_knowledge: str | None,
    file_section: str,
    pr: dict,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> str:
    return (
        file_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + file_section
        + f"\n\nPR URL: {pr.get('url', '')}\nPR number: {pr['number']}\n"
        + f"Repo: {repo_name}\nCommit: {commit_sha}"
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )


def _run_summary_review(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
) -> bool:
    pr_number = pr["number"]
    summary_instructions = (knowledge_dir / "review-summary.md").read_text(encoding="utf-8")
    vibe_heal_context = get_vibe_heal_context(config.repo.subdirs, wdir, pr["headRefName"])
    pr_description = get_pr_description(pr_number, config.repo.name, env)
    base_branch = get_pr_base_branch(pr_number, config.repo.name, env)
    commit_sha = get_pr_head_sha(pr_number, config.repo.name, env)
    files = get_changed_files(base_branch, wdir, env, expected_sha=commit_sha)

    summary_prompt = (
        summary_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + f"\n\nPR URL: {pr.get('url', '')}\nPR number: {pr['number']}\n"
        + f"Repo: {config.repo.name}\nFiles reviewed:\n"
        + "\n".join(files)
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )
    try:
        backend.run(summary_prompt, cwd=wdir)
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: summary backend timed out", pr_number)
        return False
    else:
        return True
