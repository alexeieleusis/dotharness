import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotharness.code_host import (
    ChangeRequest,
    ChangeRequestState,
    CodeHost,
    CodeHostError,
    RepositoryRef,
    ReviewState,
    UnsupportedCapabilityError,
)
from dotharness.pr_review.backend import Backend
from dotharness.pr_review.config import HarnessConfig
from dotharness.pr_review.lock import acquire_lock
from dotharness.pr_review.runners.common import (
    FatalGitError,
    backend_env,
    build_design_review_prompt,
    build_early_comment_context,
    build_file_review_section,
    build_traceability_review_prompt,
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
    has_inline_review_comments,
    has_regret_review_comment,
    has_review_summary_comment,
    has_traceability_review_comment,
    host_current_user,
    open_host,
    post_no_linked_ticket_comment,
    pr_from_url,
    preserve_reviewer_request,
    resolve_linked_tickets,
    run_pr_level_pass,
)
from dotharness.pr_review.runners.regret_review import (
    RecordingBackend,
    RegretCandidate,
    build_regret_comment_body,
    build_regret_judgment_prompt,
    find_introducing_prs,
    find_regret_candidates,
    parse_regret_judgment,
    post_regret_comment,
)

logger = logging.getLogger(__name__)


async def run(config: HarnessConfig, pr_url: str | None = None) -> None:
    with acquire_lock(config.lock_key):
        await _run_locked(config, pr_url)


async def _run_locked(config: HarnessConfig, pr_url: str | None) -> None:
    scaffold = await open_host(config)
    host, repo, env = scaffold.host, scaffold.repo, scaffold.env
    current_user = await host_current_user(host)

    if pr_url:
        change_request = await pr_from_url(host, repo, pr_url)
        prs = [] if change_request is None else [change_request]
    else:
        prs = [p for p in await _get_prs(host, repo, current_user) if p.head_reference]

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
        backend_env(config, env),
        expected_repo_name=config.repo.name,
    )
    wdir = str(config.repo.working_dir)

    for pr in prs:
        pr_number = pr.number
        files_summary_done = await _should_skip_pr(pr, host, repo, current_user)
        design_done = await has_design_review_comment(host, repo, pr_number, current_user)
        traceability_done = await has_traceability_review_comment(host, repo, pr_number, current_user)
        # The regret pass is the only config-gated PR-level pass here (off by default);
        # when disabled, regret_done is True so the gate below and the call in
        # _process_pr are both unchanged, and no extra marker check runs.
        regret_done = (
            await has_regret_review_comment(host, repo, pr_number, current_user)
            if config.regret_review.enabled
            else True
        )
        if files_summary_done and design_done and traceability_done and regret_done:
            # The correctness pipeline and all (independently tracked) PR-level passes
            # already succeeded for this PR revision — nothing left to do.
            continue
        try:
            await _process_pr(
                pr,
                config,
                host,
                repo,
                knowledge_dir,
                extra_knowledge,
                backend,
                wdir,
                env,
                current_user,
                not files_summary_done,
                PrLevelPassesDone(design=design_done, traceability=traceability_done, regret=regret_done),
            )
        except FatalGitError:
            logger.exception("PR #%d: fatal git error", pr_number)
            break
        except Exception:
            logger.exception("PR #%d: error", pr_number)


async def _get_prs(host: CodeHost, repo: RepositoryRef, login: str) -> list[ChangeRequest]:
    """Open PRs with a review requested from ``login``, sorted by number; [] when the read failed.

    One list call returns every field this runner reads (including the author the regret pass's
    ``config.regret_review.authors`` gate matches on; an authorless PR is non-matching, fail-closed).
    An unsupported capability follows the policy of ``(review_requested, ChangeRequests.list)``
    (unlisted: stop).
    """
    if not login:
        logger.warning("No login for the acting user; treating as no review-requested PRs")
        return []
    try:
        prs = await host.change_requests.list(repo, state=ChangeRequestState.OPEN, requested_reviewer=login)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError as exc:
        logger.warning("Could not list review-requested PRs for %s: %s", await host.repository.display_name(repo), exc)
        return []
    return sorted(prs, key=lambda p: p.number)


async def _has_user_approved(host: CodeHost, repo: RepositoryRef, pr_number: int, current_user: str) -> bool:
    """Whether current_user has an approving review on the PR (every review is read). A failed read counts as
    not approved."""
    try:
        reviews = await host.change_requests.reviews(repo, pr_number)
    except UnsupportedCapabilityError:
        raise
    except CodeHostError:
        return False
    return any(
        r.state is ReviewState.APPROVED and r.author is not None and r.author.login == current_user for r in reviews
    )


async def _should_skip_pr(pr: ChangeRequest, host: CodeHost, repo: RepositoryRef, current_user: str) -> bool:
    pr_number = pr.number
    if await _has_user_approved(host, repo, pr_number, current_user):
        logger.info("PR #%d already approved by self, skipping", pr_number)
        return True
    if await has_review_summary_comment(host, repo, pr_number, current_user):
        logger.info("PR #%d already has review summary comment, skipping", pr_number)
        return True
    if await has_inline_review_comments(host, repo, pr_number, current_user):
        logger.info("PR #%d has partial inline review comments, skipping", pr_number)
        return True
    return False


@dataclass(frozen=True)
class PrLevelPassesDone:
    """Whether each independently tracked PR-level pass already posted its marker for this revision."""

    design: bool
    traceability: bool
    regret: bool


async def _process_pr(
    pr: ChangeRequest,
    config: HarnessConfig,
    host: CodeHost,
    repo: RepositoryRef,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    current_user: str,
    run_files_summary: bool,
    pr_level_done: PrLevelPassesDone,
) -> None:
    pr_number = pr.number
    logger.info("PR #%d: starting", pr_number)
    original_sha = await git_detach_and_record(wdir, env)
    try:
        # The user still reviews and approves the PR themselves — this runner's job is
        # only to post automated findings, not to conclude the review on their behalf.
        # A pass here can still submit a formal review as a side effect (or race a
        # concurrent runner, e.g. review_prs's vibe_heal post, that does), which would
        # clear the current user from the PR's requested-reviewers list and hide it from
        # this and every other runner's review-requested search — restore it
        # regardless of outcome, mirroring every other "non-concluding" runner.
        async with preserve_reviewer_request(host, repo, pr_number, current_user):
            await git_fetch_and_checkout(pr.head_reference, wdir, env)
            # Gathered once and shared by all four passes below (mirrors self_review.py's
            # _gather_pr_context/_cached_file_diff) so a PR needing both the correctness
            # pipeline and a PR-level pass in the same cycle doesn't refetch PR metadata or
            # re-diff every changed file for each pass.
            ctx = await _gather_pr_context(pr, config, host, repo, wdir, env)
            if run_files_summary:
                # Only post the summary when every file review succeeded: the summary comment
                # is what _should_skip_pr treats as "done", so posting it after a partial
                # failure would permanently suppress the retry of the failed files.
                if await _run_file_reviews(pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, ctx):
                    await _run_summary_review(pr, config, knowledge_dir, extra_knowledge, backend, wdir, ctx)
                else:
                    logger.warning("PR #%d: file review(s) failed, skipping summary so the next run retries", pr_number)
            # Independent of the correctness pipeline by design: this pass's own fate
            # (and its own noop-if-already-posted check inside _run_design_review) is
            # decoupled from files/summary's, so neither one's retry forces the other's.
            # traceability and regret (when enabled) extend the same decoupling, each
            # with its own noop-if-already-posted check.
            await _run_design_review(
                pr, config, knowledge_dir, extra_knowledge, backend, wdir, env, current_user, pr_level_done.design, ctx
            )
            await _run_traceability_review(
                pr,
                config,
                knowledge_dir,
                extra_knowledge,
                backend,
                wdir,
                env,
                current_user,
                pr_level_done.traceability,
                ctx,
            )
            if config.regret_review.enabled:
                await _run_regret_review(
                    pr,
                    config,
                    knowledge_dir,
                    extra_knowledge,
                    backend,
                    wdir,
                    env,
                    current_user,
                    pr_level_done.regret,
                    ctx,
                )
    except FatalGitError:
        raise
    except Exception:
        logger.exception("PR #%d: error", pr_number)
    finally:
        await git_restore(original_sha, pr.head_reference, wdir, env)


async def _gather_pr_context(
    pr: ChangeRequest, config: HarnessConfig, host: CodeHost, repo: RepositoryRef, wdir: str, env: dict
) -> dict:
    pr_number = pr.number
    vibe_heal_context = get_vibe_heal_context(config.repo.subdirs, wdir, pr.head_reference)
    pr_description = await get_pr_description(host, repo, pr_number)
    base_branch = await get_pr_base_branch(host, repo, pr_number)
    commit_sha = await get_pr_head_sha(host, repo, pr_number)
    files = await get_changed_files(base_branch, wdir, env, expected_sha=commit_sha)
    return {
        "host": host,
        "repo": repo,
        "change_request": await get_change_request(host, repo, pr_number),
        "vibe_heal_context": vibe_heal_context,
        "pr_description": pr_description,
        "base_branch": base_branch,
        "commit_sha": commit_sha,
        "files": files,
        "diff_cache": {},
    }


async def _cached_file_diff(ctx: dict, file: str, wdir: str, env: dict) -> str:
    """The file/summary pass and the design pass both need every changed file's diff;
    caching on ctx means a PR processed by both in the same cycle only runs `git diff`
    once per file instead of twice."""
    if file not in ctx["diff_cache"]:
        ctx["diff_cache"][file] = await get_file_diff(file, ctx["base_branch"], wdir, env)
    return ctx["diff_cache"][file]


def _change_request(ctx: dict, pr_number: int) -> ChangeRequest:
    """The change request read when the PR context was gathered; a prompt that names it cannot be built without."""
    change_request = ctx["change_request"]
    if change_request is None:
        raise RuntimeError(f"PR #{pr_number}: the change request could not be read")  # noqa: TRY003
    return change_request


async def _run_file_reviews(
    pr: ChangeRequest,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    ctx: dict,
) -> bool:
    pr_number = pr.number
    file_instructions = (knowledge_dir / "review-file.md").read_text(encoding="utf-8")
    files = ctx["files"]
    commit_sha = ctx["commit_sha"]
    pr_description = ctx["pr_description"]
    vibe_heal_context = ctx["vibe_heal_context"]

    all_ok = True
    for file in files:
        diff = await _cached_file_diff(ctx, file, wdir, env)
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
            result = await backend.run(prompt, cwd=wdir, context=f"PR #{pr_number} file {file}")
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
    pr: ChangeRequest,
    repo_name: str,
    commit_sha: str,
    pr_description: str | None,
    vibe_heal_context: str | None,
) -> str:
    return (
        file_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + file_section
        + f"\n\nPR URL: {pr.url}\nPR number: {pr.number}\n"
        + f"Repo: {repo_name}\nCommit: {commit_sha}"
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )


async def _run_design_review(
    pr: ChangeRequest,
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
    stay decoupled from the file/summary pipeline's. A backend exit of 0 is not itself
    proof the marker comment was posted, so success is re-verified against the same check
    before returning True; a false positive here would make the design pass never retry.

    design_done only tells us the pass never fully completed; it can't tell a from-scratch
    retry apart from a retry after a crash that already posted some inline findings. That's
    why get_design_review_flagged_locations is fetched below on every invocation (not
    gated by design_done) and fed into the prompt — see design-review-requirements.md
    §7.1 for the partial-failure duplicate-inline-comment gap this closes.

    Orchestration itself (noop check → build prompt → run backend → re-verify marker) is
    shared with _run_traceability_review below, and with self_review.py's equivalent pair,
    via common.run_pr_level_pass; only the pieces below (instructions file, flagged-
    locations getter, prompt builder) are specific to the design pass."""
    pr_number = pr.number
    repo_name = config.repo.name

    async def build_prompt() -> str:
        design_instructions = (knowledge_dir / "review-design.md").read_text(encoding="utf-8")
        diff_sections = "".join([
            build_file_review_section(file, await _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
            for file in ctx["files"]
        ])
        prior_flagged_locations = await get_design_review_flagged_locations(
            ctx["host"], ctx["repo"], pr_number, current_user
        )
        return build_design_review_prompt(
            design_instructions,
            extra_knowledge,
            diff_sections,
            _change_request(ctx, pr_number),
            pr_number,
            repo_name,
            ctx["commit_sha"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
            prior_flagged_locations,
        )

    return bool(
        await run_pr_level_pass(
            pr_number,
            backend,
            wdir,
            is_done=design_done,
            build_prompt=build_prompt,
            has_comment_fn=lambda: has_design_review_comment(ctx["host"], ctx["repo"], pr_number, current_user),
            label="design review",
        )
    )


async def _run_traceability_review(
    pr: ChangeRequest,
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
    pr_number = pr.number
    repo_name = config.repo.name
    comment_cache: dict = {}
    resolved: dict = {}

    async def short_circuit() -> bool | None:
        tickets = await resolve_linked_tickets(ctx["host"], ctx["repo"], _change_request(ctx, pr_number), comment_cache)
        if tickets is None:
            logger.info("PR #%d: linked ticket lookup was inconclusive (API failure) — will retry next run", pr_number)
            return False
        if not tickets:
            if await post_no_linked_ticket_comment(ctx["host"], ctx["repo"], pr_number):
                return True
            logger.error("PR #%d: failed to post 'no linked ticket found' comment", pr_number)
            return False
        resolved["tickets"] = tickets
        return None

    async def build_prompt() -> str:
        traceability_instructions = (knowledge_dir / "review-traceability.md").read_text(encoding="utf-8")
        diff_sections = "".join([
            build_file_review_section(file, await _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
            for file in ctx["files"]
        ])
        prior_flagged_locations = await get_traceability_review_flagged_locations(
            ctx["host"], ctx["repo"], pr_number, current_user
        )
        change_request = _change_request(ctx, pr_number)
        early_comment_context = await build_early_comment_context(
            ctx["host"], ctx["repo"], pr_number, change_request.created_at, comment_cache
        )
        return build_traceability_review_prompt(
            traceability_instructions,
            extra_knowledge,
            diff_sections,
            change_request,
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
        await run_pr_level_pass(
            pr_number,
            backend,
            wdir,
            is_done=traceability_done,
            build_prompt=build_prompt,
            has_comment_fn=lambda: has_traceability_review_comment(ctx["host"], ctx["repo"], pr_number, current_user),
            label="traceability review",
            short_circuit=short_circuit,
        )
    )


async def _run_regret_review(
    pr: ChangeRequest,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    env: dict,
    current_user: str,
    regret_done: bool,
    ctx: dict,
) -> bool:
    """No persisted state exists for this runner, so has_regret_review_comment (checked
    once by the caller and passed in as regret_done) is the *only* idempotency signal
    (unlike self_review.py, which treats the equivalent check as defense-in-depth on top
    of its own persisted regret_reviewed_prs state). If a marked comment from a previous
    attempt is already present, this is a noop: the candidate pipeline and the backend
    aren't invoked again, and the pass counts as already succeeded — this is what lets
    the regret pass's own retry stay decoupled from the others.

    Unlike the other passes in this runner, the backend posts nothing here (review-
    regret.md): it replies with one verdict line per candidate, and this wrapper parses
    those lines (regret_review.parse_regret_judgment) and posts the regret comment
    itself when any verdict is YES (regret_review.build_regret_comment_body). The
    backend object is therefore wrapped in regret_review.RecordingBackend, since
    common.run_pr_level_pass — whose orchestration (noop check → build prompt → run
    backend → re-verify) is reused unchanged — keeps no copy of the reply. The
    re-verification after a post uses the plain-bool has_regret_review_comment rather
    than self_review.py's tri-state check_regret_review_comment_status: with no
    persisted state there is nothing to protect from an inconclusive check.

    The candidate-finding pipeline (find_introducing_prs → find_regret_candidates) runs as
    run_pr_level_pass's short_circuit hook. When it comes up empty, the pass makes no
    backend call and posts nothing (regret-review-requirements.md §7.2 step 4). In this
    stateless runner that outcome counts as complete (the hook returns True, unlike
    self_review.py's equivalent, which returns False and leaves the pass unmarked in its
    persisted state): nothing is persisted here, so a later PR
    update that changes the blamed lines still gets a fresh candidate search when the PR
    is next processed, while blocking the gate on the structurally empty case — the
    common one — would leave the PR on the review-requested queue, re-running this pass's
    bounded git/host sequence, every cycle. A backend or post failure, by contrast, still
    returns False, so the PR stays queued for a retry."""
    pr_number = pr.number
    repo_name = config.repo.name
    candidates: list[RegretCandidate] = []
    reply: dict[str, str] = {}

    async def short_circuit() -> bool | None:
        budget_start = time.monotonic()
        introducing_hunks = await find_introducing_prs(
            ctx["host"], ctx["repo"], _change_request(ctx, pr_number), config, wdir, env, budget_start
        )
        found = await find_regret_candidates(introducing_hunks, config, wdir, env, budget_start)
        if not found:
            logger.debug("PR #%d: no regret candidates — nothing to judge this run", pr_number)
            return True
        candidates.extend(found)
        return None

    async def build_prompt() -> str:
        regret_instructions = (knowledge_dir / "review-regret.md").read_text(encoding="utf-8")
        diff_sections = "".join([
            build_file_review_section(file, await _cached_file_diff(ctx, file, wdir, env), os.path.join(wdir, file))
            for file in ctx["files"]
        ])
        return build_regret_judgment_prompt(
            regret_instructions,
            extra_knowledge,
            candidates,
            diff_sections,
            _change_request(ctx, pr_number),
            pr_number,
            repo_name,
            ctx["commit_sha"],
            ctx["pr_description"],
            ctx["vibe_heal_context"],
        )

    async def has_comment_fn() -> bool:
        findings = parse_regret_judgment(reply["reply"], candidates)
        if not findings:
            logger.info("PR #%d: regret review found no confirmed regret — nothing to post", pr_number)
            return True
        if not await post_regret_comment(ctx["host"], ctx["repo"], pr_number, build_regret_comment_body(findings)):
            logger.error("PR #%d: failed to post the regret comment — will retry next run", pr_number)
            return False
        return await has_regret_review_comment(ctx["host"], ctx["repo"], pr_number, current_user)

    return bool(
        await run_pr_level_pass(
            pr_number,
            cast(Backend, RecordingBackend(backend, reply)),
            wdir,
            is_done=regret_done,
            build_prompt=build_prompt,
            has_comment_fn=has_comment_fn,
            label="regret review",
            short_circuit=short_circuit,
        )
    )


async def _run_summary_review(
    pr: ChangeRequest,
    config: HarnessConfig,
    knowledge_dir: Path,
    extra_knowledge: str | None,
    backend: Backend,
    wdir: str,
    ctx: dict,
) -> bool:
    pr_number = pr.number
    summary_instructions = (knowledge_dir / "review-summary.md").read_text(encoding="utf-8")
    files = ctx["files"]
    pr_description = ctx["pr_description"]
    vibe_heal_context = ctx["vibe_heal_context"]

    summary_prompt = (
        summary_instructions
        + (f"\n\n## Additional Review Guide\n{extra_knowledge}" if extra_knowledge else "")
        + f"\n\nPR URL: {pr.url}\nPR number: {pr.number}\n"
        + f"Repo: {config.repo.name}\nFiles reviewed:\n"
        + "\n".join(files)
        + (f"\n\n{pr_description}" if pr_description else "")
        + (f"\n\n## Static Analysis\n{vibe_heal_context}" if vibe_heal_context else "")
    )
    try:
        result = await backend.run(summary_prompt, cwd=wdir, context=f"PR #{pr_number} summary")
        if result.returncode != 0:
            logger.error("PR #%d: summary backend exited %d", pr_number, result.returncode)
            return False
    except subprocess.TimeoutExpired:
        logger.exception("PR #%d: summary backend timed out", pr_number)
        return False
    else:
        return True
