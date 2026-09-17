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
    build_design_review_prompt,
    build_file_review_section,
    build_subprocess_env,
    check_design_review_comment_status,
    check_review_summary_comment_status,
    get_changed_files,
    get_current_user,
    get_design_review_flagged_locations,
    get_file_diff,
    get_gh_token,
    get_pr_base_branch,
    get_pr_description,
    get_pr_head_sha,
    get_vibe_heal_context,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    has_design_review_comment,
    run_cmd,
)

logger = logging.getLogger(__name__)


def run(config: HarnessConfig) -> None:
    with acquire_lock(config.repo_slug):
        _run_locked(config)


def _get_extra_knowledge(config) -> str | None:
    if config.harness.review_knowledge_file and config.harness.review_knowledge_file.exists():
        return config.harness.review_knowledge_file.read_text(encoding="utf-8")
    return None


def _build_backend(config, env: dict) -> Backend:
    token = env.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set in the subprocess environment")  # noqa: TRY003
    return Backend(
        config.harness.backend,
        config.harness.backend_timeout_seconds,
        config.harness.path_prepend,
        {**config.harness.env, "GITHUB_TOKEN": token},
        expected_repo_name=config.repo.name,
    )


def _should_skip_pr(number: int, repo: str, current_user: str, reviewed: set, env: dict) -> bool | None:
    """Returns True to skip (confirmed done), False to process, or None if the comment
    check was inconclusive (API failure) and the caller should defer judgment."""
    if number in reviewed:
        return True
    return check_review_summary_comment_status(number, repo, current_user, env)


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
        "diff_cache": {},
    }


def _cached_file_diff(ctx: dict, file: str, wdir: str, env: dict) -> str:
    """The file+summary pass and the design pass both need every changed file's diff;
    caching on ctx means a PR processed by both in the same cycle only runs `git diff`
    once per file instead of twice."""
    if file not in ctx["diff_cache"]:
        ctx["diff_cache"][file] = get_file_diff(file, ctx["base_branch"], wdir, env)
    return ctx["diff_cache"][file]


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
        result = backend.run(prompt, cwd=wdir, context=f"PR #{number} file {file}")
        if result.returncode != 0:
            logger.error("PR #%d file %s: backend exited %d", number, file, result.returncode)
            return True
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d file %s: backend timed out", number, file)
        return True
    return False


def _review_files(
    ctx: dict,
    wdir: str,
    env: dict,
    backend: Backend,
    file_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    partial_files: set[str],
    repo_slug: str,
) -> tuple[bool, set[str]]:
    files = ctx["files"]
    commit_sha = ctx["commit_sha"]
    pr_description = ctx["pr_description"]
    vibe_heal_context = ctx["vibe_heal_context"]
    any_failure = False
    for file in files:
        if file in partial_files:
            logger.info("PR #%d: file %s already reviewed in a prior run — skipping", number, file)
            continue
        diff = _cached_file_diff(ctx, file, wdir, env)
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
        else:
            partial_files.add(file)
            state.set_partial_reviewed_files(repo_slug, number, list(partial_files))
    return any_failure, partial_files


def _build_design_prompt(
    design_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    ctx: dict,
    wdir: str,
    current_user: str,
    env: dict,
) -> str:
    diff_sections = "".join(
        build_file_review_section(file, _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
        for file in ctx["files"]
    )
    prior_flagged_locations = get_design_review_flagged_locations(number, config.repo.name, current_user, env)
    return build_design_review_prompt(
        design_instructions,
        extra_knowledge,
        diff_sections,
        pr,
        number,
        config.repo.name,
        ctx["commit_sha"],
        ctx["pr_description"],
        ctx["vibe_heal_context"],
        prior_flagged_locations,
    )


def _run_design_review(
    design_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    ctx: dict,
    backend: Backend,
    wdir: str,
    current_user: str,
    env: dict,
) -> None:
    """Tracked independently of reviewed_prs/partial_reviews (state.design_reviewed_prs)
    so this pass's own retry never forces, or is forced by, the per-file review's
    retry-from-scratch behavior. The upfront comment check is an optimization to skip a
    redundant backend run; the comment check after the backend run is the one that
    actually gates state.add_design_reviewed_pr, since a returncode of 0 only means the
    backend exited cleanly, not that it actually posted — review_requested.py, which has
    no persisted state at all, relies on the equivalent check as its *only* signal."""
    if has_design_review_comment(number, config.repo.name, current_user, env):
        state.add_design_reviewed_pr(config.repo_slug, number)
        return
    design_prompt = _build_design_prompt(
        design_instructions, extra_knowledge, pr, number, config, ctx, wdir, current_user, env
    )
    try:
        result = backend.run(design_prompt, cwd=wdir, context=f"PR #{number} design review")
        if result.returncode != 0:
            logger.error("PR #%d: design review backend exited %d", number, result.returncode)
            return
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: design review backend timed out", number)
        return
    comment_status = check_design_review_comment_status(number, config.repo.name, current_user, env)
    if comment_status is None:
        logger.warning(
            "PR #%d: could not confirm design review comment status (comment check failed) — "
            "treating as unconfirmed rather than assuming it's missing",
            number,
        )
        return
    if not comment_status:
        logger.error(
            "PR #%d: design review backend exited 0 but no design review comment found on GitHub — treating as failure",
            number,
        )
        return
    state.add_design_reviewed_pr(config.repo_slug, number)


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
    current_user: str,
    env: dict,
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
        result = backend.run(summary_prompt, cwd=wdir, context=f"PR #{number} summary")
        if result.returncode != 0:
            logger.error("PR #%d: summary backend exited %d", number, result.returncode)
            return True
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: summary backend timed out", number)
        return True
    comment_status = check_review_summary_comment_status(number, config.repo.name, current_user, env)
    if comment_status is None:
        logger.warning(
            "PR #%d: could not confirm summary comment status (comment check failed) — "
            "treating as unconfirmed rather than assuming it's missing",
            number,
        )
        return True
    if not comment_status:
        logger.error(
            "PR #%d: summary backend exited 0 but no summary comment found on GitHub — treating as failure",
            number,
        )
        return True
    logger.info("PR #%d: summary comment confirmed", number)
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
    design_instructions: str,
    extra_knowledge: str | None,
    reviewed: set,
    original_sha: str,
    partial_files: set[str],
    current_user: str,
    run_files_summary: bool,
    run_design: bool,
) -> None:
    try:
        ctx = _gather_pr_context(pr, number, config, wdir, env)
        if run_files_summary:
            file_failure, partial_files = _review_files(
                ctx,
                wdir,
                env,
                backend,
                file_instructions,
                extra_knowledge,
                pr,
                number,
                config,
                partial_files,
                config.repo_slug,
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
                current_user,
                env,
            )
            if not file_failure and not summary_failure:
                reviewed.add(number)
                sr_state = state.read_self_review_state(config.repo_slug)
                sr_state["partial_reviews"].pop(str(number), None)
                state.write_self_review_state(config.repo_slug, list(reviewed), sr_state["partial_reviews"])
            elif not file_failure:
                state.set_partial_reviewed_files(config.repo_slug, number, list(partial_files))
        if run_design:
            _run_design_review(
                design_instructions, extra_knowledge, pr, number, config, ctx, backend, wdir, current_user, env
            )
    except Exception:
        logger.exception("PR #%d: error", number)
    finally:
        git_restore(original_sha, pr["headRefName"], wdir, env)


def _run_locked(config: HarnessConfig) -> None:
    gh_token = get_gh_token(config.harness.gh_token_cmd)
    env = build_subprocess_env(config.harness.path_prepend, config.harness.env, gh_token)
    current_user = get_current_user(env)

    prs = _list_my_prs(config.repo.name, env)
    if prs is None:
        logger.warning("Failed to fetch open PRs; skipping this cycle without touching self_review state")
        return
    pruned_state = state.prune_self_review_state(config.repo_slug, {p["number"] for p in prs})
    reviewed = set(pruned_state["reviewed_prs"])
    design_reviewed = set(pruned_state["design_reviewed_prs"])

    knowledge_dir = Path(config.harness.knowledge_dir) / "pr-review"
    file_instructions = (knowledge_dir / "review-file.md").read_text(encoding="utf-8")
    summary_instructions = (knowledge_dir / "review-summary.md").read_text(encoding="utf-8")
    design_instructions = (knowledge_dir / "review-design.md").read_text(encoding="utf-8")
    extra_knowledge = _get_extra_knowledge(config)
    backend = _build_backend(config, env)
    wdir = str(config.repo.working_dir)
    original_sha = git_detach_and_record(wdir, env)

    for pr in prs:
        number = pr["number"]
        run_design = number not in design_reviewed
        skip = _should_skip_pr(number, config.repo.name, current_user, reviewed, env)
        run_files_summary = skip is False
        if skip is None:
            logger.warning(
                "PR #%d: could not confirm review status (comment check failed) — "
                "skipping file/summary review this cycle without marking reviewed",
                number,
            )
        elif skip and number not in reviewed:
            reviewed.add(number)
            sr_state = state.read_self_review_state(config.repo_slug)
            sr_state["partial_reviews"].pop(str(number), None)
            state.write_self_review_state(config.repo_slug, list(reviewed), sr_state["partial_reviews"])

        if not run_files_summary and not run_design:
            continue

        logger.info("PR #%d: starting", number)
        partial_files = set(state.get_partial_reviewed_files(config.repo_slug, number))
        _process_single_pr(
            pr,
            number,
            config,
            wdir,
            env,
            backend,
            file_instructions,
            summary_instructions,
            design_instructions,
            extra_knowledge,
            reviewed,
            original_sha,
            partial_files,
            current_user,
            run_files_summary,
            run_design,
        )


def _list_my_prs(repo: str, env: dict) -> list[dict] | None:
    """Returns None (rather than []) on fetch failure, so callers can distinguish "gh pr list
    failed" from "confirmed zero open PRs" instead of treating a transient API hiccup as an
    authoritative empty set."""
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
            "number,url,headRefName,createdAt,closingIssuesReferences",
            "--limit",
            "500",
        ],
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return sorted(json.loads(result.stdout), key=lambda p: p["number"])
    except ValueError:
        logger.exception("_list_my_prs: malformed JSON from gh CLI")
        return None
