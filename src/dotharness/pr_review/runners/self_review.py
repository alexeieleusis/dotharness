import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotharness.code_host import ChangeRequest, CodeHost, RepositoryRef
from dotharness.gh import gh_argv
from dotharness.pr_review import state
from dotharness.pr_review.backend import Backend
from dotharness.pr_review.config import HarnessConfig
from dotharness.pr_review.lock import acquire_lock
from dotharness.pr_review.runners.common import (
    TIMEOUT_GH,
    LinkedTicket,
    build_design_review_prompt,
    build_early_comment_context,
    build_file_review_section,
    build_traceability_review_prompt,
    check_design_review_comment_status,
    check_regret_review_comment_status,
    check_review_summary_comment_status,
    check_traceability_review_comment_status,
    get_change_request,
    get_changed_files,
    get_design_review_flagged_locations,
    get_file_diff,
    get_pr_base_branch,
    get_pr_description,
    get_pr_head_sha,
    get_traceability_review_flagged_locations,
    get_vibe_heal_context,
    git_detach_and_record,
    git_fetch_and_checkout,
    git_restore,
    has_design_review_comment,
    has_regret_review_comment,
    has_traceability_review_comment,
    host_current_user,
    open_host,
    post_no_linked_ticket_comment,
    preserve_reviewer_request,
    resolve_linked_tickets,
    run_cmd,
    run_pr_level_pass,
)
from dotharness.pr_review.runners.regret_review import (
    RecordingBackend,
    RegretCandidate,
    build_regret_comment_body,
    build_regret_judgment_prompt,
    find_introducing_prs,
    find_regret_candidates,
    is_complete_judgment_response,
    parse_regret_judgment,
    post_regret_comment,
)

logger = logging.getLogger(__name__)


async def run(config: HarnessConfig) -> None:
    with acquire_lock(config.lock_key):
        await _run_locked(config)


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


async def _should_skip_pr(
    number: int, host: CodeHost, repo: RepositoryRef, current_user: str, reviewed: set
) -> bool | None:
    """Returns True to skip (confirmed done), False to process, or None if the comment
    check was inconclusive (API failure) and the caller should defer judgment."""
    if number in reviewed:
        return True
    return await check_review_summary_comment_status(host, repo, number, current_user)


async def _gather_pr_context(
    pr: dict, number: int, config, host: CodeHost, repo: RepositoryRef, wdir: str, env: dict
) -> dict:
    await git_fetch_and_checkout(pr["headRefName"], wdir, env)
    vibe_heal_context = get_vibe_heal_context(config.repo.subdirs, wdir, pr["headRefName"])
    pr_description = await get_pr_description(host, repo, number)
    base_branch = await get_pr_base_branch(host, repo, number)
    commit_sha = await get_pr_head_sha(host, repo, number)
    files = await get_changed_files(base_branch, wdir, env, expected_sha=commit_sha)
    return {
        "host": host,
        "repo": repo,
        "change_request": await get_change_request(host, repo, number),
        "vibe_heal_context": vibe_heal_context,
        "pr_description": pr_description,
        "base_branch": base_branch,
        "commit_sha": commit_sha,
        "files": files,
        "diff_cache": {},
    }


def _change_request(ctx: dict, number: int) -> ChangeRequest:
    """The change request read when the PR context was gathered; a prompt that names it cannot be built without."""
    change_request = ctx["change_request"]
    if change_request is None:
        raise RuntimeError(f"PR #{number}: the change request could not be read")  # noqa: TRY003
    return change_request


async def _cached_file_diff(ctx: dict, file: str, wdir: str, env: dict) -> str:
    """The file+summary pass and the design pass both need every changed file's diff;
    caching on ctx means a PR processed by both in the same cycle only runs `git diff`
    once per file instead of twice."""
    if file not in ctx["diff_cache"]:
        ctx["diff_cache"][file] = await get_file_diff(file, ctx["base_branch"], wdir, env)
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


async def _review_file(
    prompt: str,
    backend: Backend,
    wdir: str,
    number: int,
    file: str,
) -> bool:
    try:
        result = await backend.run(prompt, cwd=wdir, context=f"PR #{number} file {file}")
        if result.returncode != 0:
            logger.error("PR #%d file %s: backend exited %d", number, file, result.returncode)
            return True
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d file %s: backend timed out", number, file)
        return True
    return False


async def _review_files(
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
        diff = await _cached_file_diff(ctx, file, wdir, env)
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
        if await _review_file(prompt, backend, wdir, number, file):
            any_failure = True
        else:
            partial_files.add(file)
            state.set_partial_reviewed_files(repo_slug, number, list(partial_files))
    return any_failure, partial_files


async def _build_design_prompt(
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
    diff_sections = "".join([
        build_file_review_section(file, await _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
        for file in ctx["files"]
    ])
    prior_flagged_locations = await get_design_review_flagged_locations(ctx["host"], ctx["repo"], number, current_user)
    return build_design_review_prompt(
        design_instructions,
        extra_knowledge,
        diff_sections,
        _change_request(ctx, number),
        number,
        config.repo.name,
        ctx["commit_sha"],
        ctx["pr_description"],
        ctx["vibe_heal_context"],
        prior_flagged_locations,
    )


async def _run_design_review(
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
    no persisted state at all, relies on the equivalent check as its *only* signal.

    Orchestration itself (noop check → build prompt → run backend → re-verify marker) is
    shared with _run_traceability_review below, and with review_requested.py's equivalent
    pair, via common.run_pr_level_pass; this wrapper only adds the persisted-state
    mutation run_pr_level_pass has no reason to know about."""
    is_done = await has_design_review_comment(ctx["host"], ctx["repo"], number, current_user)
    result = await run_pr_level_pass(
        number,
        backend,
        wdir,
        is_done=is_done,
        build_prompt=lambda: _build_design_prompt(
            design_instructions, extra_knowledge, pr, number, config, ctx, wdir, current_user, env
        ),
        has_comment_fn=lambda: check_design_review_comment_status(ctx["host"], ctx["repo"], number, current_user),
        label="design review",
    )
    if result:
        state.add_design_reviewed_pr(config.repo_slug, number)


async def _build_traceability_prompt(
    traceability_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    ctx: dict,
    wdir: str,
    current_user: str,
    env: dict,
    tickets: list[LinkedTicket],
    comment_cache: dict,
) -> str:
    diff_sections = "".join([
        build_file_review_section(file, await _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
        for file in ctx["files"]
    ])
    prior_flagged_locations = await get_traceability_review_flagged_locations(
        ctx["host"], ctx["repo"], number, current_user
    )
    change_request = _change_request(ctx, number)
    early_comment_context = await build_early_comment_context(
        ctx["host"], ctx["repo"], number, change_request.created_at, comment_cache
    )
    return build_traceability_review_prompt(
        traceability_instructions,
        extra_knowledge,
        diff_sections,
        change_request,
        number,
        config.repo.name,
        ctx["commit_sha"],
        ctx["pr_description"],
        ctx["vibe_heal_context"],
        prior_flagged_locations,
        tickets,
        early_comment_context,
    )


async def _run_traceability_review(
    traceability_instructions: str,
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
    """Tracked independently of reviewed_prs/partial_reviews/design_reviewed_prs
    (state.traceability_reviewed_prs), mirroring _run_design_review exactly
    (requirement-traceability-requirements.md §7.4). If no linked ticket can be resolved,
    the "no ticket" outcome is posted directly (no backend invocation — there's nothing
    for a model to judge) and is terminal (§7.3/§11.1): it is never retried, even if a
    ticket link is added to the PR later.

    That short-circuit is threaded into common.run_pr_level_pass's `short_circuit` hook;
    a resolved ticket list is stashed in `resolved` for build_prompt to pick up once
    short_circuit itself declines to handle the pass (returns None, meaning "a ticket was
    found, proceed normally")."""
    is_done = await has_traceability_review_comment(ctx["host"], ctx["repo"], number, current_user)
    comment_cache: dict = {}
    resolved: dict = {}

    async def short_circuit() -> bool | None:
        tickets = await resolve_linked_tickets(ctx["host"], ctx["repo"], _change_request(ctx, number), comment_cache)
        if tickets is None:
            logger.info("PR #%d: linked ticket lookup was inconclusive (API failure) — will retry next run", number)
            return False
        if not tickets:
            if await post_no_linked_ticket_comment(ctx["host"], ctx["repo"], number):
                return True
            logger.error("PR #%d: failed to post 'no linked ticket found' comment — will retry next run", number)
            return False
        resolved["tickets"] = tickets
        return None

    result = await run_pr_level_pass(
        number,
        backend,
        wdir,
        is_done=is_done,
        build_prompt=lambda: _build_traceability_prompt(
            traceability_instructions,
            extra_knowledge,
            pr,
            number,
            config,
            ctx,
            wdir,
            current_user,
            env,
            resolved["tickets"],
            comment_cache,
        ),
        has_comment_fn=lambda: check_traceability_review_comment_status(ctx["host"], ctx["repo"], number, current_user),
        label="traceability review",
        short_circuit=short_circuit,
    )
    if result:
        state.add_traceability_reviewed_pr(config.repo_slug, number)


async def _run_regret_review(
    regret_instructions: str,
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
    """Tracked independently of reviewed_prs/partial_reviews/design_reviewed_prs/
    traceability_reviewed_prs (state.regret_reviewed_prs), mirroring _run_design_review
    (regret-review-requirements.md §7.3): this pass's own retry never forces, or is
    forced by, any other pass's retry. The upfront comment check is an optimization to
    skip a redundant run; on a rerun, a PR that already carries a regret comment makes
    zero git or gh calls for this pass beyond that check itself.

    Unlike the other passes, the backend posts nothing here (review-regret.md): it
    replies with one verdict line per candidate, and this wrapper parses those lines
    (regret_review.parse_regret_judgment) and posts the regret comment itself when any
    verdict is YES (regret_review.build_regret_comment_body). The backend object is
    therefore wrapped in a reply recorder, since common.run_pr_level_pass — whose
    orchestration (noop check → build prompt → run backend → re-verify) is reused
    unchanged — keeps no copy of the reply. A run whose verdict lines confirm no regret
    at all still counts as complete: there is nothing to post, and the PR is marked
    done like any other success. A reply that does not carry a complete set of verdict
    lines (garbage, truncated, or prose with no CANDIDATE lines) is not a confirmed
    outcome: has_comment_fn returns None for it, run_pr_level_pass treats that as
    inconclusive, and the PR is left unmarked so the next cycle re-judges it instead of
    persisting a permanent "no regret" from an unconfirmed reply.

    When the candidate-finding pipeline (find_introducing_prs →
    find_regret_candidates) comes up empty, the pass makes no backend call and posts
    nothing (regret-review-requirements.md §7.2 step 4). Unlike traceability's
    "no linked ticket" outcome, that skip is not terminal: the PR is not marked
    regret-reviewed, so a later PR update that changes the blamed lines gets a fresh
    candidate search on the next run."""
    is_done = await has_regret_review_comment(ctx["host"], ctx["repo"], number, current_user)
    candidates: list[RegretCandidate] = []
    reply: dict[str, str] = {}

    async def short_circuit() -> bool | None:
        budget_start = time.monotonic()
        introducing_hunks = await find_introducing_prs(
            ctx["host"], ctx["repo"], _change_request(ctx, number), config, wdir, env, budget_start
        )
        found = await find_regret_candidates(introducing_hunks, config, wdir, env, budget_start)
        if not found:
            logger.debug("PR #%d: no regret candidates — nothing to judge this run", number)
            return False
        candidates.extend(found)
        return None

    async def build_prompt() -> str:
        diff_sections = "".join([
            build_file_review_section(file, await _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
            for file in ctx["files"]
        ])
        return build_regret_judgment_prompt(
            regret_instructions,
            extra_knowledge,
            candidates,
            diff_sections,
            _change_request(ctx, number),
            number,
            config.repo.name,
            ctx["commit_sha"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
        )

    async def has_comment_fn() -> bool | None:
        if not is_complete_judgment_response(reply["reply"], candidates):
            logger.warning(
                "PR #%d: regret review reply was not a complete judgment — no parseable verdict "
                "line for every candidate, so nothing was actually confirmed. Treating as "
                "inconclusive (will retry next cycle) rather than a confirmed miss.",
                number,
            )
            return None
        findings = parse_regret_judgment(reply["reply"], candidates)
        if not findings:
            logger.info("PR #%d: regret review found no confirmed regret — nothing to post", number)
            return True
        if not await post_regret_comment(ctx["host"], ctx["repo"], number, build_regret_comment_body(findings)):
            logger.error("PR #%d: failed to post the regret comment — will retry next run", number)
            return False
        return await check_regret_review_comment_status(ctx["host"], ctx["repo"], number, current_user)

    result = await run_pr_level_pass(
        number,
        cast(Backend, RecordingBackend(backend, reply)),
        wdir,
        is_done=is_done,
        build_prompt=build_prompt,
        has_comment_fn=has_comment_fn,
        label="regret review",
        short_circuit=short_circuit,
    )
    if result:
        state.add_regret_reviewed_pr(config.repo_slug, number)


async def _run_summary(
    summary_instructions: str,
    extra_knowledge: str | None,
    pr: dict,
    number: int,
    config,
    host: CodeHost,
    repo: RepositoryRef,
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
        result = await backend.run(summary_prompt, cwd=wdir, context=f"PR #{number} summary")
        if result.returncode != 0:
            logger.error("PR #%d: summary backend exited %d", number, result.returncode)
            return True
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: summary backend timed out", number)
        return True
    comment_status = await check_review_summary_comment_status(host, repo, number, current_user)
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


@dataclass
class _CycleContext:
    """Fields that stay constant across every PR processed in a single `_run_locked` pass."""

    config: HarnessConfig
    wdir: str
    env: dict
    backend: Backend
    file_instructions: str
    summary_instructions: str
    design_instructions: str
    traceability_instructions: str
    regret_instructions: str
    extra_knowledge: str | None
    reviewed: set
    original_sha: str
    current_user: str
    host: CodeHost
    repo: RepositoryRef


@dataclass
class _PassFlags:
    """Which review passes to run for a single PR this cycle."""

    files_summary: bool
    design: bool
    traceability: bool
    regret: bool


async def _process_single_pr(
    pr: dict,
    number: int,
    cycle: _CycleContext,
    partial_files: set[str],
    flags: _PassFlags,
) -> None:
    config = cycle.config
    wdir = cycle.wdir
    env = cycle.env
    backend = cycle.backend
    extra_knowledge = cycle.extra_knowledge
    reviewed = cycle.reviewed
    current_user = cycle.current_user
    try:
        # A PR can be assigned to the user (not just authored by them — see
        # _list_my_prs), in which case GitHub *does* allow the assignor to also
        # request their review, unlike the author-can't-review-own-PR case this
        # runner otherwise relies on. Guard the same way as every other
        # "non-concluding" runner: a review submitted here as a side effect (or by a
        # concurrent runner racing this PR) shouldn't silently drop the user from the
        # requested-reviewers list.
        async with preserve_reviewer_request(cycle.host, cycle.repo, number, current_user):
            ctx = await _gather_pr_context(pr, number, config, cycle.host, cycle.repo, wdir, env)
            if flags.files_summary:
                file_failure, partial_files = await _review_files(
                    ctx,
                    wdir,
                    env,
                    backend,
                    cycle.file_instructions,
                    extra_knowledge,
                    pr,
                    number,
                    config,
                    partial_files,
                    config.repo_slug,
                )
                summary_failure = await _run_summary(
                    cycle.summary_instructions,
                    extra_knowledge,
                    pr,
                    number,
                    config,
                    cycle.host,
                    cycle.repo,
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
            if flags.design:
                await _run_design_review(
                    cycle.design_instructions,
                    extra_knowledge,
                    pr,
                    number,
                    config,
                    ctx,
                    backend,
                    wdir,
                    current_user,
                    env,
                )
            if flags.traceability:
                await _run_traceability_review(
                    cycle.traceability_instructions,
                    extra_knowledge,
                    pr,
                    number,
                    config,
                    ctx,
                    backend,
                    wdir,
                    current_user,
                    env,
                )
            if flags.regret:
                await _run_regret_review(
                    cycle.regret_instructions,
                    extra_knowledge,
                    pr,
                    number,
                    config,
                    ctx,
                    backend,
                    wdir,
                    current_user,
                    env,
                )
    except Exception:
        logger.exception("PR #%d: error", number)
    finally:
        await git_restore(cycle.original_sha, pr["headRefName"], wdir, env)


async def _run_locked(config: HarnessConfig) -> None:
    scaffold = await open_host(config)
    host, repo, env = scaffold.host, scaffold.repo, scaffold.env
    current_user = await host_current_user(host)

    prs = await _list_my_prs(config.repo.name, env)
    if prs is None:
        logger.warning("Failed to fetch open PRs; skipping this cycle without touching self_review state")
        return
    pruned_state = state.prune_self_review_state(config.repo_slug, {p["number"] for p in prs})
    reviewed = set(pruned_state["reviewed_prs"])
    design_reviewed = set(pruned_state["design_reviewed_prs"])
    traceability_reviewed = set(pruned_state["traceability_reviewed_prs"])
    regret_reviewed = set(pruned_state["regret_reviewed_prs"])

    knowledge_dir = Path(config.harness.knowledge_dir) / "pr-review"
    file_instructions = (knowledge_dir / "review-file.md").read_text(encoding="utf-8")
    summary_instructions = (knowledge_dir / "review-summary.md").read_text(encoding="utf-8")
    design_instructions = (knowledge_dir / "review-design.md").read_text(encoding="utf-8")
    traceability_instructions = (knowledge_dir / "review-traceability.md").read_text(encoding="utf-8")
    regret_instructions = (
        (knowledge_dir / "review-regret.md").read_text(encoding="utf-8") if config.regret_review.enabled else ""
    )
    extra_knowledge = _get_extra_knowledge(config)
    backend = _build_backend(config, env)
    wdir = str(config.repo.working_dir)
    original_sha = await git_detach_and_record(wdir, env)
    cycle = _CycleContext(
        config=config,
        wdir=wdir,
        env=env,
        backend=backend,
        file_instructions=file_instructions,
        summary_instructions=summary_instructions,
        design_instructions=design_instructions,
        traceability_instructions=traceability_instructions,
        regret_instructions=regret_instructions,
        extra_knowledge=extra_knowledge,
        reviewed=reviewed,
        original_sha=original_sha,
        current_user=current_user,
        host=host,
        repo=repo,
    )

    for pr in prs:
        number = pr["number"]
        run_design = number not in design_reviewed
        run_traceability = number not in traceability_reviewed
        run_regret = config.regret_review.enabled and number not in regret_reviewed
        skip = await _should_skip_pr(number, host, repo, current_user, reviewed)
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

        if not run_files_summary and not run_design and not run_traceability and not run_regret:
            continue

        logger.info("PR #%d: starting", number)
        partial_files = set(state.get_partial_reviewed_files(config.repo_slug, number))
        await _process_single_pr(
            pr,
            number,
            cycle,
            partial_files,
            _PassFlags(run_files_summary, run_design, run_traceability, run_regret),
        )


async def _list_prs_by_flag(repo: str, env: dict, filter_flag: str) -> list[dict] | None:
    """Returns None (rather than []) on fetch failure, so callers can distinguish "gh pr list
    failed" from "confirmed zero open PRs" instead of treating a transient API hiccup as an
    authoritative empty set."""
    result = await run_cmd(
        gh_argv(
            "pr",
            "list",
            "--repo",
            repo,
            filter_flag,
            "@me",
            "--state",
            "open",
            "--json",
            "number,url,headRefName,createdAt,closingIssuesReferences,author",
            "--limit",
            "500",
        ),
        cwd="/",
        env=env,
        timeout=TIMEOUT_GH,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        logger.exception("_list_my_prs: malformed JSON from gh CLI")
        return None


async def _list_my_prs(repo: str, env: dict) -> list[dict] | None:
    """PRs the user is responsible for self-reviewing: authored by them, or assigned to
    them (e.g. a bot-authored PR opened on their behalf), deduplicated by number. Returns
    None if either underlying query fails (see _list_prs_by_flag), rather than treating a
    partial result from only one of the two as the authoritative set."""
    by_number: dict[int, dict] = {}
    for flag in ("--author", "--assignee"):
        prs = await _list_prs_by_flag(repo, env, flag)
        if prs is None:
            return None
        for pr in prs:
            by_number.setdefault(pr["number"], pr)
    return sorted(by_number.values(), key=lambda p: p["number"])
