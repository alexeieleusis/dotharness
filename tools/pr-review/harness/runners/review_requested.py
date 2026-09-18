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
    FatalGitError,
    build_design_review_prompt,
    build_early_comment_context,
    build_file_review_section,
    build_subprocess_env,
    build_traceability_review_prompt,
    get_changed_files,
    get_current_user,
    get_design_review_flagged_locations,
    get_file_diff,
    get_gh_token,
    get_pr_base_branch,
    get_pr_description,
    get_pr_head_sha,
    get_traceability_review_flagged_locations,
    get_vibe_heal_context,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    has_design_review_comment,
    has_inline_review_comments,
    has_review_summary_comment,
    has_traceability_review_comment,
    post_no_linked_ticket_comment,
    pr_from_url,
    remove_reviewer,
    resolve_linked_tickets,
    run_cmd,
    run_pr_level_pass,
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
        prs = [pr_from_url(pr_url, config.repo.name, env, "number,url,headRefName,createdAt,closingIssuesReferences")]
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
        expected_repo_name=config.repo.name,
    )
    wdir = str(config.repo.working_dir)

    for pr in prs:
        pr_number = pr["number"]
        files_summary_done = _should_skip_pr(pr, config.repo.name, current_user, env)
        design_done = has_design_review_comment(pr_number, config.repo.name, current_user, env)
        traceability_done = has_traceability_review_comment(pr_number, config.repo.name, current_user, env)
        if files_summary_done and design_done and traceability_done:
            # The correctness pipeline and both (independently tracked) PR-level passes
            # already succeeded for this PR revision — nothing left to do.
            continue
        try:
            _process_pr(
                pr,
                config,
                knowledge_dir,
                extra_knowledge,
                backend,
                wdir,
                env,
                current_user,
                not files_summary_done,
                design_done,
                traceability_done,
            )
        except FatalGitError:
            logger.exception("PR #%d: fatal git error", pr_number)
            break
        except Exception:
            logger.exception("PR #%d: error", pr_number)


def _get_prs(repo: str, env: dict) -> list[dict]:
    # Use gh pr list with search predicates to get all fields in a single call,
    # avoiding the N+1 pattern of gh search prs + individual gh pr view calls.
    result = run_cmd(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            "user-review-requested:@me",
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
        return []
    return sorted(json.loads(result.stdout), key=lambda p: p["number"])


def _has_user_approved(pr_number: int, repo: str, current_user: str, env: dict) -> bool:
    page = 1
    while True:
        result = run_cmd(
            [
                "gh",
                "api",
                "--method",
                "GET",
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
        if not reviews or not isinstance(reviews, list):
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
    run_files_summary: bool,
    design_done: bool,
    traceability_done: bool,
) -> None:
    pr_number = pr["number"]
    logger.info("PR #%d: starting", pr_number)
    original_sha = git_detach_and_record(wdir, env)
    try:
        git_fetch_and_checkout(pr["headRefName"], wdir, env)
        # Gathered once and shared by all four passes below (mirrors self_review.py's
        # _gather_pr_context/_cached_file_diff) so a PR needing both the correctness
        # pipeline and a PR-level pass in the same cycle doesn't refetch PR metadata or
        # re-diff every changed file for each pass.
        ctx = _gather_pr_context(pr, config, wdir, env)
        if run_files_summary:
            files_ok = _run_file_reviews(pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, ctx)
            summary_ok = _run_summary_review(pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, ctx)
        else:
            files_ok = True
            summary_ok = True
        # Independent of files_ok/summary_ok by design: this pass's own fate (and its
        # own noop-if-already-posted check inside _run_design_review) is decoupled from
        # the correctness pipeline's, so neither one's retry forces the other's. This is
        # the same AND-gate the code comment here used to name as a future reuse point
        # (issue #4) — traceability_ok below extends it the same way, with its own `_ok`
        # boolean rather than a parallel gating mechanism.
        design_ok = _run_design_review(
            pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, current_user, design_done, ctx
        )
        traceability_ok = _run_traceability_review(
            pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, current_user, traceability_done, ctx
        )
        if files_ok and summary_ok and design_ok and traceability_ok:
            remove_reviewer(pr_number, config.repo.name, current_user, env)
    except Exception:
        logger.exception("PR #%d: error", pr_number)
    finally:
        git_restore(original_sha, pr["headRefName"], wdir, env)


def _gather_pr_context(pr: dict, config: HarnessConfig, wdir: str, env: dict) -> dict:
    pr_number = pr["number"]
    vibe_heal_context = get_vibe_heal_context(config.repo.subdirs, wdir, pr["headRefName"])
    pr_description = get_pr_description(pr_number, config.repo.name, env)
    base_branch = get_pr_base_branch(pr_number, config.repo.name, env)
    commit_sha = get_pr_head_sha(pr_number, config.repo.name, env)
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
    """The file/summary pass and the design pass both need every changed file's diff;
    caching on ctx means a PR processed by both in the same cycle only runs `git diff`
    once per file instead of twice."""
    if file not in ctx["diff_cache"]:
        ctx["diff_cache"][file] = get_file_diff(file, ctx["base_branch"], wdir, env)
    return ctx["diff_cache"][file]


def _run_file_reviews(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    ctx: dict,
) -> bool:
    pr_number = pr["number"]
    file_instructions = (knowledge_dir / "review-file.md").read_text(encoding="utf-8")
    files = ctx["files"]
    commit_sha = ctx["commit_sha"]
    pr_description = ctx["pr_description"]
    vibe_heal_context = ctx["vibe_heal_context"]

    all_ok = True
    for file in files:
        diff = _cached_file_diff(ctx, file, wdir, env)
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
            result = backend.run(prompt, cwd=wdir, context=f"PR #{pr_number} file {file}")
            if result.returncode != 0:
                logger.error("PR #%d file %s: backend exited %d", pr_number, file, result.returncode)
                all_ok = False
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


def _run_design_review(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    current_user: str,
    design_done: bool,
    ctx: dict,
) -> bool:
    """No persisted state exists for this runner, so has_design_review_comment (checked
    once by the caller and passed in as design_done) is the *only* idempotency signal
    (unlike self_review.py, which treats the equivalent check as defense-in-depth on top
    of its own persisted design_reviewed_prs state). If a marked comment from a previous
    attempt is already present, this is a noop: the backend isn't invoked again, and the
    pass counts as already succeeded — this is what lets the design pass's own retry
    stay decoupled from files_ok/summary_ok. A backend exit of 0 is not itself proof the
    marker comment was posted, so success is re-verified against the same check before
    returning True; a false positive here would make remove_reviewer fire and the design
    pass never retry.

    design_done only tells us the pass never fully completed; it can't tell a from-scratch
    retry apart from a retry after a crash that already posted some inline findings. That's
    why get_design_review_flagged_locations is fetched below on every invocation (not
    gated by design_done) and fed into the prompt — see design-review-requirements.md
    §7.1 for the partial-failure duplicate-inline-comment gap this closes.

    Orchestration itself (noop check → build prompt → run backend → re-verify marker) is
    shared with _run_traceability_review below, and with self_review.py's equivalent pair,
    via common.run_pr_level_pass; only the pieces below (instructions file, flagged-
    locations getter, prompt builder) are specific to the design pass."""
    pr_number = pr["number"]
    repo_name = config.repo.name

    def build_prompt() -> str:
        design_instructions = (knowledge_dir / "review-design.md").read_text(encoding="utf-8")
        diff_sections = "".join(
            build_file_review_section(file, _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
            for file in ctx["files"]
        )
        prior_flagged_locations = get_design_review_flagged_locations(pr_number, repo_name, current_user, env)
        return build_design_review_prompt(
            design_instructions,
            extra_knowledge,
            diff_sections,
            pr,
            pr_number,
            repo_name,
            ctx["commit_sha"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
            prior_flagged_locations,
        )

    return bool(
        run_pr_level_pass(
            pr_number,
            backend,
            wdir,
            is_done=design_done,
            build_prompt=build_prompt,
            has_comment_fn=lambda: has_design_review_comment(pr_number, repo_name, current_user, env),
            label="design review",
        )
    )


def _run_traceability_review(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    current_user: str,
    traceability_done: bool,
    ctx: dict,
) -> bool:
    """Mirrors _run_design_review exactly (requirement-traceability-requirements.md §7.5):
    no persisted state exists in this runner, so has_traceability_review_comment (checked
    once by the caller and passed in as traceability_done) is the only idempotency signal.

    One difference from the design pass: when resolve_linked_tickets finds no ticket via
    either mechanism, the "no linked ticket found" outcome is posted directly (§7.3) — no
    backend invocation, since there's nothing for a model to judge — and that comment
    posting itself (not a marker re-check) is what determines success here. That short-
    circuit is threaded into common.run_pr_level_pass's `short_circuit` hook; a resolved
    ticket list is stashed in `resolved` for build_prompt to pick up once short_circuit
    itself declines to handle the pass (returns None, meaning "a ticket was found, proceed
    normally")."""
    pr_number = pr["number"]
    repo_name = config.repo.name
    comment_cache: dict = {}
    resolved: dict = {}

    def short_circuit() -> bool | None:
        tickets = resolve_linked_tickets(pr, repo_name, env, comment_cache)
        if tickets is None:
            logger.info("PR #%d: linked ticket lookup was inconclusive (API failure) — will retry next run", pr_number)
            return False
        if not tickets:
            if post_no_linked_ticket_comment(pr_number, repo_name, env):
                return True
            logger.error("PR #%d: failed to post 'no linked ticket found' comment", pr_number)
            return False
        resolved["tickets"] = tickets
        return None

    def build_prompt() -> str:
        traceability_instructions = (knowledge_dir / "review-traceability.md").read_text(encoding="utf-8")
        diff_sections = "".join(
            build_file_review_section(file, _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
            for file in ctx["files"]
        )
        prior_flagged_locations = get_traceability_review_flagged_locations(pr_number, repo_name, current_user, env)
        early_comment_context = build_early_comment_context(
            pr_number, repo_name, pr.get("createdAt", ""), env, comment_cache
        )
        return build_traceability_review_prompt(
            traceability_instructions,
            extra_knowledge,
            diff_sections,
            pr,
            pr_number,
            repo_name,
            ctx["commit_sha"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
            prior_flagged_locations,
            resolved["tickets"],
            early_comment_context,
        )

    return bool(
        run_pr_level_pass(
            pr_number,
            backend,
            wdir,
            is_done=traceability_done,
            build_prompt=build_prompt,
            has_comment_fn=lambda: has_traceability_review_comment(pr_number, repo_name, current_user, env),
            label="traceability review",
            short_circuit=short_circuit,
        )
    )


def _run_summary_review(
    pr: dict,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    ctx: dict,
) -> bool:
    pr_number = pr["number"]
    summary_instructions = (knowledge_dir / "review-summary.md").read_text(encoding="utf-8")
    files = ctx["files"]
    pr_description = ctx["pr_description"]
    vibe_heal_context = ctx["vibe_heal_context"]

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
        result = backend.run(summary_prompt, cwd=wdir, context=f"PR #{pr_number} summary")
        if result.returncode != 0:
            logger.error("PR #%d: summary backend exited %d", pr_number, result.returncode)
            return False
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: summary backend timed out", pr_number)
        return False
    else:
        return True
