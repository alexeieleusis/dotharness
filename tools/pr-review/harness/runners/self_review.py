import json
import logging
import os
import subprocess
from pathlib import Path

from harness import state
from harness.backend import Backend
from harness.config import HarnessConfig
from harness.lock import acquire_lock
from harness.runners.common import (
    TIMEOUT_GH,
    build_file_review_section,
    build_subprocess_env,
    get_changed_files,
    get_file_diff,
    get_gh_token,
    get_pr_base_branch,
    get_pr_description,
    get_pr_head_sha,
    get_vibe_heal_context,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    run_cmd,
)

logger = logging.getLogger(__name__)
OSC_REVIEW_MARKERS = ("osc-review", "Review Summary")


def run(config: HarnessConfig) -> None:
    with acquire_lock(config.repo_slug):
        _run_locked(config)


def _get_extra_knowledge(config) -> str | None:
    if config.harness.review_knowledge_file and config.harness.review_knowledge_file.exists():
        return config.harness.review_knowledge_file.read_text(encoding="utf-8")
    return None


def _build_backend(config, env: dict) -> Backend:
    return Backend(
        config.harness.backend,
        config.harness.backend_timeout_seconds,
        config.harness.path_prepend,
        {**config.harness.env, "GITHUB_TOKEN": env.get("GITHUB_TOKEN", "")},
    )


def _should_skip_pr(number: int, repo: str, reviewed: set, env: dict) -> bool:
    if number in reviewed:
        return True
    return bool(_has_osc_review_comment(number, repo, env))


def _gather_pr_context(pr: dict, number: int, config, wdir: str, env: dict) -> dict:
    git_fetch_and_checkout(pr["headRefName"], wdir, env)
    vibe_heal_context = get_vibe_heal_context(config.repo.subdirs, wdir, pr["headRefName"])
    pr_description = get_pr_description(number, config.repo.name, env)
    base_branch = get_pr_base_branch(number, config.repo.name, env)
    commit_sha = get_pr_head_sha(number, config.repo.name, env)
    files = get_changed_files(base_branch, wdir, env, expected_sha=commit_sha)
    return {
        "vibe_heal_context": vibe_heal_context,
        "pr_description": pr_description,
        "base_branch": base_branch,
        "commit_sha": commit_sha,
        "files": files,
    }


def _build_file_review_prompt(
    file: str,
    diff: str,
    abs_path: str,
    file_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> str:
    file_section = build_file_review_section(file, diff, abs_path)
    return (
        file_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + file_section
        + f"\n\nPR URL: {pr.get('url', '')}\nPR number: {number}\n"
        + f"Repo: {config.repo.name}\nCommit: {commit_sha}"
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )


def _review_file(
    prompt: str,
    backend: Backend,
    wdir: str,
    number: int,
    file: str,
) -> bool:
    try:
        result = backend.run(prompt, cwd=wdir)
        if result.returncode != 0:
            logger.error("PR #%d file %s: backend exited %d", number, file, result.returncode)
            return True
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d file %s: backend timed out", number, file)
        return True
    return False


def _review_files(
    files: list[str],
    base_branch: str,
    wdir: str,
    env: dict,
    backend: Backend,
    file_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> bool:
    any_failure = False
    for file in files:
        diff = get_file_diff(file, base_branch, wdir, env)
        abs_path = os.path.join(wdir, file)
        prompt = _build_file_review_prompt(
            file,
            diff,
            abs_path,
            file_instructions,
            extra_knowledge,
            pr,
            number,
            config,
            commit_sha,
            pr_description,
            vibe_heal_context,
        )
        if _review_file(prompt, backend, wdir, number, file):
            any_failure = True
    return any_failure


def _run_summary(
    summary_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    files: list[str],
    pr_description: str | None,
    vibe_heal_context: str | None,
    backend: Backend,
    wdir: str,
) -> bool:
    summary_prompt = (
        summary_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + f"\n\nPR URL: {pr.get('url', '')}\nPR number: {number}\n"
        + f"Repo: {config.repo.name}\nFiles reviewed:\n"
        + "\n".join(files)
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )
    try:
        result = backend.run(summary_prompt, cwd=wdir)
        if result.returncode != 0:
            logger.error("PR #%d: summary backend exited %d", number, result.returncode)
            return True
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: summary backend timed out", number)
        return True
    return False


def _process_single_pr(
    pr: dict,
    number: int,
    config,
    wdir: str,
    env: dict,
    backend: Backend,
    file_instructions: str,
    summary_instructions: str,
    extra_knowledge: str | None,
    reviewed: set,
    original_sha: str,
) -> None:
    try:
        ctx = _gather_pr_context(pr, number, config, wdir, env)
        file_failure = _review_files(
            ctx["files"],
            ctx["base_branch"],
            wdir,
            env,
            backend,
            file_instructions,
            extra_knowledge,
            pr,
            number,
            config,
            ctx["commit_sha"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
        )
        summary_failure = _run_summary(
            summary_instructions,
            extra_knowledge,
            pr,
            number,
            config,
            ctx["files"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
            backend,
            wdir,
        )
        if not file_failure and not summary_failure:
            reviewed.add(number)
            state.write_self_review_state(config.repo_slug, list(reviewed))
    except Exception:
        logger.exception("PR #%d: error", number)
    finally:
        git_restore(original_sha, pr["headRefName"], wdir, env)


def _run_locked(config: HarnessConfig) -> None:
    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)

    reviewed = set(state.read_self_review_state(config.repo_slug)["reviewed_prs"])
    prs = _list_my_prs(config.repo.name, env)

    knowledge_dir = Path(config.harness.knowledge_dir) / "pr-review"
    file_instructions = (knowledge_dir / "review-file.md").read_text(encoding="utf-8")
    summary_instructions = (knowledge_dir / "review-summary.md").read_text(encoding="utf-8")
    extra_knowledge = _get_extra_knowledge(config)
    backend = _build_backend(config, env)
    wdir = str(config.repo.working_dir)
    original_sha = git_detach_and_record(wdir, env)

    for pr in prs:
        number = pr["number"]
        if _should_skip_pr(number, config.repo.name, reviewed, env):
            if number not in reviewed:
                reviewed.add(number)
                state.write_self_review_state(config.repo_slug, list(reviewed))
            continue
        _process_single_pr(
            pr,
            number,
            config,
            wdir,
            env,
            backend,
            file_instructions,
            summary_instructions,
            extra_knowledge,
            reviewed,
            original_sha,
        )


def _list_my_prs(repo: str, env: dict) -> list[dict]:
    result = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--author",
            "@me",
            "--state",
            "open",
            "--json",
            "number,url,headRefName",
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
    return sorted(json.loads(result.stdout), key=lambda p: p["number"])


def _has_osc_review_comment(pr_number: int, repo: str, env: dict) -> bool:
    result = run_cmd(
        ["gh", "api", f"repos/{repo}/issues/{pr_number}/comments", "--paginate"],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return False
    for c in json.loads(result.stdout):
        body = c.get("body", "").lstrip("# ").strip()
        if any(body.startswith(m) for m in OSC_REVIEW_MARKERS):
            return True
    return False
