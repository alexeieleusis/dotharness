from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from spec_prism_flow.build import agent_runner, resume_state, vibe_heal_integration
from spec_prism_flow.build.completion_log import CompletionRecord
from spec_prism_flow.build.errors import EmptyImplementationError, OrchestrationError
from spec_prism_flow.build.gh_ops import PRHandle
from spec_prism_flow.build.git_ops import DiffStat, GitCommandError, origin_url
from spec_prism_flow.build.resume_state import ResumeState
from spec_prism_flow.build.retry_budget import RetryBudget
from spec_prism_flow.build.toolchain import (
    BASE_BRANCH,
    Toolchain,
    build_dry_run_toolchain,
    build_live_toolchain,
)

if TYPE_CHECKING:
    from pathlib import Path

    from spec_prism_flow.config import SpecPrismFlowConfig
    from spec_prism_flow.phase_file import PhaseFile

_BASE_REF = f"origin/{BASE_BRANCH}"
_ADDRESS_COMMENTS_COMMIT_MESSAGE = "Address review comments"


@dataclass(frozen=True)
class PhaseRunResult:
    phase_number: int
    merged: bool
    completion_record: CompletionRecord


def _repo_slug(clone: Path) -> str:
    """`"owner/name"`, derived from `clone`'s `origin` remote -- mirrors
    `claude_backend`'s own repo-identity derivation, adapted to keep the owner
    segment (gh_ops/resume_state need the full `"owner/name"` slug, not just the repo
    name). Handles both `git@host:owner/name.git` and `https://host/owner/name.git`
    forms by normalizing `:` to `/` before splitting."""
    normalized = origin_url(clone).strip().replace(":", "/").removesuffix("/").removesuffix(".git")
    parts = [p for p in normalized.split("/") if p]
    return "/".join(parts[-2:])


@dataclass
class _Progress:
    """Whatever's known about this run so far -- mutated in place by
    `_implement_or_resume`/`_iterate` as each step completes, and read by
    `_partial_record` for the best-effort escalation write, whatever amount of it
    happens to be filled in at the point an `OrchestrationError` is raised."""

    pr: PRHandle | None = None
    pr_opened_at: datetime | None = None
    cycle_index: int = 0
    manual_test_first_try_pass: bool | None = None
    address_comments_cycles: int = 0
    diff: DiffStat | None = None
    previously_seen_fingerprints: set[vibe_heal_integration.Fingerprint] = field(default_factory=set)


def _implement_or_resume(
    toolchain: Toolchain,
    clone: Path,
    phase: PhaseFile,
    branch: str,
    repo: str,
    state_path: Path,
    resume: bool,
    progress: _Progress,
) -> None:
    """Step 1 ("Implement") and step 2 ("Push + PR"), or -- when `resume` finds an
    existing, still-open PR for this phase -- neither: reuse that PR and pick up the
    iterate loop where the persisted resume state left off."""
    existing_state = resume_state.load_resume_state(state_path) if resume else None
    if existing_state is not None:
        status = toolchain.pr_view(repo, existing_state.pr_number)
        if status.state == "OPEN":
            progress.pr = PRHandle(number=existing_state.pr_number, url=existing_state.pr_url)
            progress.cycle_index = existing_state.cycle_index
            return

    toolchain.checkout_fresh_branch(clone, branch, BASE_BRANCH)
    toolchain.agent_run(agent_runner.build_prompt(phase), clone)
    if not toolchain.commit_all(clone, agent_runner.commit_message(phase)):
        raise EmptyImplementationError()

    changed = toolchain.diff_paths(clone, _BASE_REF)
    toolchain.scope_check(changed, phase.scope, _BASE_REF)

    toolchain.push_branch(clone, branch)
    toolchain.fetch_resync(clone, branch)
    pr = toolchain.pr_create(clone, branch, agent_runner.commit_message(phase), agent_runner.build_prompt(phase))
    progress.pr = pr
    progress.pr_opened_at = datetime.now(UTC)
    progress.cycle_index = 0
    resume_state.save_resume_state(
        state_path,
        ResumeState(repo=repo, branch=branch, pr_number=pr.number, pr_url=pr.url, cycle_index=0),
    )


def _run_static_analysis_cycle(toolchain: Toolchain, clone: Path, progress: _Progress) -> None:
    report = toolchain.static_analysis_scan(clone)
    if report is None:
        return
    current_fingerprints = vibe_heal_integration.fingerprints_from_report(report)
    if vibe_heal_integration.should_post(current_fingerprints, progress.previously_seen_fingerprints):
        toolchain.static_analysis_post(clone)
    progress.previously_seen_fingerprints |= current_fingerprints


def _run_review_cycle(toolchain: Toolchain, clone: Path, branch: str, progress: _Progress) -> None:
    toolchain.review_self_review(clone)
    toolchain.review_address_comments(clone)
    progress.address_comments_cycles += 1
    if toolchain.commit_all(clone, _ADDRESS_COMMENTS_COMMIT_MESSAGE):
        toolchain.push_branch(clone, branch)


def _iterate(
    toolchain: Toolchain,
    clone: Path,
    config: SpecPrismFlowConfig,
    phase: PhaseFile,
    branch: str,
    repo: str,
    state_path: Path,
    strict: bool,
    progress: _Progress,
) -> None:
    """Steps 3-6 (the iterate loop): one shared `RetryBudget`, checked before each
    cycle's work using whatever cycle_index/unresolved-thread-count is already known
    from the previous pass (0 before the first). Loops again on unresolved review
    threads or a retryable failed manual test; otherwise falls through to step 7."""
    pr = cast(PRHandle, progress.pr)
    pr_number = pr.number
    pr_url = pr.url
    retry_budget = RetryBudget(max_cycles=config.build.max_retry_cycles)
    last_unresolved_thread_count = 0

    while True:
        retry_budget.check(progress.cycle_index, pr_url=pr_url, unresolved_thread_count=last_unresolved_thread_count)

        toolchain.fetch_resync(clone, branch)
        changed = toolchain.diff_paths(clone, _BASE_REF)
        toolchain.scope_check(changed, phase.scope, _BASE_REF)

        if config.vibe_heal.enabled:
            _run_static_analysis_cycle(toolchain, clone, progress)

        if config.review.enabled:
            _run_review_cycle(toolchain, clone, branch, progress)

        progress.cycle_index += 1
        resume_state.save_resume_state(
            state_path,
            ResumeState(repo=repo, branch=branch, pr_number=pr_number, pr_url=pr_url, cycle_index=progress.cycle_index),
        )

        last_unresolved_thread_count = toolchain.unresolved_thread_count(repo, pr_number)
        if last_unresolved_thread_count > 0:
            continue

        outcome = toolchain.manual_test_prompt(phase.number, phase.name, phase.manual_test_checklist, strict)
        if progress.manual_test_first_try_pass is None:
            progress.manual_test_first_try_pass = outcome.passed
        if outcome.retry:
            continue
        return


def _partial_record(phase: PhaseFile, progress: _Progress, escalation_reason: str) -> CompletionRecord:
    diff = progress.diff or DiffStat(files=0, lines_added=0, lines_removed=0)
    return CompletionRecord(
        phase_number=phase.number,
        phase_name=phase.name,
        pr_number=progress.pr.number if progress.pr else None,
        pr_url=progress.pr.url if progress.pr else None,
        pr_opened_at=progress.pr_opened_at,
        pr_merged_at=None,
        manual_test_first_try_pass=progress.manual_test_first_try_pass,
        escalation_reason=escalation_reason,
        address_comments_cycles=progress.address_comments_cycles,
        pr_diff_files=diff.files,
        pr_diff_lines_added=diff.lines_added,
        pr_diff_lines_removed=diff.lines_removed,
        human_escalations=1,
    )


def run_phase(
    clone: Path,
    config: SpecPrismFlowConfig,
    phase: PhaseFile,
    *,
    toolchain: Toolchain | None = None,
    dry_run: bool = False,
    auto_merge: bool = True,
    resume: bool = False,
    strict: bool = False,
) -> PhaseRunResult:
    """Runs one phase end-to-end: implement, push, iterate, merge -- behind the shared
    `Toolchain` seam. `toolchain`, when omitted, defaults to
    `build_dry_run_toolchain()`/`build_live_toolchain(config)` per `dry_run`, so a
    caller normally only threads `dry_run` through; callers that need finer control
    (tests, or manual exercise of a specific escalation) pass `toolchain` explicitly.
    On any `OrchestrationError`, a best-effort partial completion-log write is
    attempted (its own failure swallowed) before the original error re-raises
    unchanged.
    """
    if toolchain is None:
        toolchain = build_dry_run_toolchain() if dry_run else build_live_toolchain(config)

    repo = _repo_slug(clone)
    branch = agent_runner.branch_name(phase)
    state_path = resume_state.resume_state_path(config, repo, branch)
    progress = _Progress()

    try:
        _implement_or_resume(toolchain, clone, phase, branch, repo, state_path, resume, progress)
        _iterate(toolchain, clone, config, phase, branch, repo, state_path, strict, progress)

        progress.diff = toolchain.diff_stat(clone, _BASE_REF)
        toolchain.merge_gates_run(clone, config.build.commands)

        pr = cast(PRHandle, progress.pr)
        completion_record = CompletionRecord(
            phase_number=phase.number,
            phase_name=phase.name,
            pr_number=pr.number,
            pr_url=pr.url,
            pr_opened_at=progress.pr_opened_at,
            pr_merged_at=None,
            manual_test_first_try_pass=progress.manual_test_first_try_pass,
            escalation_reason=None,
            address_comments_cycles=progress.address_comments_cycles,
            pr_diff_files=progress.diff.files,
            pr_diff_lines_added=progress.diff.lines_added,
            pr_diff_lines_removed=progress.diff.lines_removed,
            human_escalations=0,
        )

        if not auto_merge:
            return PhaseRunResult(phase_number=phase.number, merged=False, completion_record=completion_record)

        toolchain.pr_merge(repo, pr.number)
        merged_record = replace(completion_record, pr_merged_at=datetime.now(UTC))
        resume_state.clear_resume_state(state_path)
        with contextlib.suppress(GitCommandError):
            toolchain.completion_log_append(clone, merged_record)
        return PhaseRunResult(phase_number=phase.number, merged=True, completion_record=merged_record)

    except OrchestrationError as exc:
        with contextlib.suppress(Exception):
            toolchain.completion_log_append(clone, _partial_record(phase, progress, str(exc)))
        raise
