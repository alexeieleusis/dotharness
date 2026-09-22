from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from spec_prism_flow.build import (
    agent_runner,
    completion_log,
    gh_ops,
    git_ops,
    harness_integration,
    manual_test,
    merge_gates,
    scope_guard,
    vibe_heal_integration,
)
from spec_prism_flow.build.completion_log import CompletionRecord
from spec_prism_flow.build.gh_ops import PRHandle, PRStatus
from spec_prism_flow.build.git_ops import DiffStat
from spec_prism_flow.build.manual_test import ManualTestOutcome

if TYPE_CHECKING:
    from spec_prism_flow.config import SpecPrismFlowConfig


@dataclass(frozen=True)
class Toolchain:
    """One field per external effect `run_phase` needs, so live execution and
    `--dry-run` can share `phase_runner.run_phase`'s control flow against two totally
    different backing implementations. Field signatures mirror their real
    dependency-phase counterparts as closely as possible; where a dependency's
    function takes something `run_phase` doesn't have on hand at the point it would
    naturally call it (e.g. `gh_ops`'s `repo: str` for a static-analysis path derived
    from `config`), the field is instead a small closure built by
    `build_live_toolchain` that supplies it internally."""

    checkout_fresh_branch: Callable[[Path, str, str], None]
    agent_run: Callable[[str, Path], str]
    commit_all: Callable[[Path, str], bool]
    diff_paths: Callable[[Path, str], list[str]]
    scope_check: Callable[[list[str], list[str], str], None]
    push_branch: Callable[[Path, str], None]
    fetch_resync: Callable[[Path, str], None]
    pr_create: Callable[[Path, str, str, str], PRHandle]
    pr_view: Callable[[str, int], PRStatus]
    static_analysis_scan: Callable[[Path], dict | None]
    static_analysis_post: Callable[[Path], None]
    review_self_review: Callable[[Path], str]
    review_address_comments: Callable[[Path], str]
    unresolved_thread_count: Callable[[str, int], int]
    manual_test_prompt: Callable[[int, str, list[str], bool], ManualTestOutcome]
    diff_stat: Callable[[Path, str], DiffStat]
    merge_gates_run: Callable[[Path, list[str]], None]
    pr_merge: Callable[[str, int], None]
    completion_log_append: Callable[[Path, CompletionRecord], None]


# Fixed, repo-relative locations for the shared completion log -- it's a tracked file
# meant to live in the target repo itself (alongside docs/phases/), not somewhere
# derived from `config`, since any clone of that repo must find it at the same path.
_COMPLETION_LOG_JSON_RELPATH = Path("docs/completion-log.json")
_COMPLETION_LOG_MD_RELPATH = Path("docs/completion-log.md")

# The base branch every step operates against. Not config-driven -- BuildConfig
# (spec_prism_flow/config.py) has no base-branch field, and every dependency-phase
# default (git_ops.checkout_fresh_branch's `base`, diff_name_only's `base_ref`)
# already hardcodes "main"/"origin/main", so this mirrors that rather than inventing
# new config surface out of this phase's scope.
BASE_BRANCH = "main"


def _vibe_heal_paths(config: SpecPrismFlowConfig, clone: Path) -> tuple[Path, Path]:
    """`vibe_heal_integration.scan`/`post` require caller-supplied `report_path`/
    `env_path` -- VibeHealConfig carries no default location for either. These must
    live outside `clone` (not e.g. under a dotfile inside it): `commit_all` stages
    everything unconditionally (`git add -A`), so a scan artifact written inside the
    clone would get swept into the phase's own diff and trip the next cycle's
    `scope_check`. Keyed by `clone.name` under `config.build.state_dir` so concurrent
    phases (distinct clones) never collide."""
    scratch_dir = config.build.state_dir / "vibe-heal" / clone.name
    return scratch_dir / "report.json", scratch_dir / "env"


def build_live_toolchain(config: SpecPrismFlowConfig) -> Toolchain:
    """Wires every field to its real dependency-phase implementation."""
    backend = agent_runner.build_backend(config.agent)

    def _static_analysis_scan(clone: Path) -> dict | None:
        report_path, env_path = _vibe_heal_paths(config, clone)
        return vibe_heal_integration.scan(config.vibe_heal, clone, report_path, env_path)

    def _static_analysis_post(clone: Path) -> None:
        report_path, env_path = _vibe_heal_paths(config, clone)
        vibe_heal_integration.post(config.vibe_heal, clone, report_path, env_path)

    def _review_self_review(clone: Path) -> str:
        return harness_integration.self_review(config.review, clone)

    def _review_address_comments(clone: Path) -> str:
        return harness_integration.address_comments(config.review, clone)

    def _manual_test_prompt(
        phase_number: int, phase_name: str, checklist: list[str], strict: bool
    ) -> ManualTestOutcome:
        return manual_test.prompt(phase_number, phase_name, checklist, strict=strict)

    def _completion_log_append(clone: Path, record: CompletionRecord) -> None:
        completion_log.append_and_commit(
            clone,
            clone / _COMPLETION_LOG_JSON_RELPATH,
            clone / _COMPLETION_LOG_MD_RELPATH,
            record,
            base_branch=BASE_BRANCH,
        )

    return Toolchain(
        checkout_fresh_branch=git_ops.checkout_fresh_branch,
        agent_run=backend.invoke,
        commit_all=git_ops.commit_all,
        diff_paths=git_ops.diff_name_only,
        scope_check=scope_guard.check,
        push_branch=git_ops.push_branch,
        fetch_resync=git_ops.fetch_resync,
        pr_create=gh_ops.pr_create,
        pr_view=gh_ops.pr_view,
        static_analysis_scan=_static_analysis_scan,
        static_analysis_post=_static_analysis_post,
        review_self_review=_review_self_review,
        review_address_comments=_review_address_comments,
        unresolved_thread_count=gh_ops.unresolved_thread_count,
        manual_test_prompt=_manual_test_prompt,
        diff_stat=git_ops.diff_stat,
        merge_gates_run=merge_gates.run_merge_gates,
        pr_merge=gh_ops.pr_merge,
        completion_log_append=_completion_log_append,
    )


def build_dry_run_toolchain() -> Toolchain:
    """Stubs every field so `--dry-run` can exercise `run_phase`'s full branch/commit/
    scope/retry control flow against a disposable scratch repo with zero live
    subprocess dependencies. `commit_all` returns `True` on its first call and `False`
    on every call after that (per-instance state via a fresh counter each call to this
    function), simulating "agent implemented something, then address-comments
    converged with nothing left to commit." Every other stub is a fixed, deterministic
    value chosen so a dry run completes end-to-end without blocking on interactive
    input or looping forever: `manual_test_prompt` always passes on the first try,
    `unresolved_thread_count` always reports none outstanding, and `pr_view` always
    reports the PR as still open (so a `resume=True` dry run exercises the
    skip-steps-1-2 path). A caller who wants to manually exercise the retry-budget-
    exhaustion or resume-mid-loop paths overrides individual fields on the returned
    `Toolchain` (see this phase's PR description / manual test notes) rather than
    relying on these defaults."""
    commit_calls = itertools.count()

    def _commit_all(clone: Path, message: str) -> bool:
        return next(commit_calls) == 0

    def _manual_test_prompt(
        phase_number: int, phase_name: str, checklist: list[str], strict: bool
    ) -> ManualTestOutcome:
        return ManualTestOutcome(passed=True, retry=False, notes=None)

    return Toolchain(
        checkout_fresh_branch=lambda clone, branch, base: None,
        agent_run=lambda instructions, cwd: "dry-run: no agent invoked",
        commit_all=_commit_all,
        diff_paths=lambda clone, base_ref: ["dry-run-change.txt"],
        scope_check=lambda changed_paths, scope_globs, base_ref: None,
        push_branch=lambda clone, branch: None,
        fetch_resync=lambda clone, branch: None,
        pr_create=lambda clone, branch, title, body: PRHandle(number=1, url="https://example.invalid/pull/1"),
        pr_view=lambda repo, pr_number: PRStatus(state="OPEN", mergeable="MERGEABLE", review_decision="APPROVED"),
        static_analysis_scan=lambda clone: None,
        static_analysis_post=lambda clone: None,
        review_self_review=lambda clone: "",
        review_address_comments=lambda clone: "",
        unresolved_thread_count=lambda repo, pr_number: 0,
        manual_test_prompt=_manual_test_prompt,
        diff_stat=lambda clone, base_ref: DiffStat(files=1, lines_added=1, lines_removed=0),
        merge_gates_run=lambda clone, commands: None,
        pr_merge=lambda repo, pr_number: None,
        completion_log_append=lambda clone, record: None,
    )
