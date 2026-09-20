# Phase 11 — Phase-run orchestrator

*Purpose: run one phase end-to-end — implement, push, iterate, merge — behind one seam that live and dry-run execution both share.*

## Scope
- spec_prism_flow/build/toolchain.py
- spec_prism_flow/build/phase_runner.py
- tests/build/test_toolchain.py
- tests/build/test_phase_runner.py
- tests/build/test_phase_runner_escalations.py

## Requirements

Excerpt, chunk A-3-3-4's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 `Toolchain` (`spec_prism_flow/build/toolchain.py`)
Dataclass fields, one per external effect: `checkout_fresh_branch`, `agent_run` (Phase 07's `AgentBackend.invoke`), `commit_all`, `diff_paths` (Phase 07's `git_ops.diff_name_only`, changed paths vs the base ref — the precomputed list `scope_check` consumes), `scope_check` (Phase 06's `scope_guard.check`, adapted to take a pre-computed diff-paths list per that signature rather than computing the diff itself), `push_branch`, `fetch_resync`, `pr_create`, `pr_view` (Phase 08), `static_analysis_scan`, `static_analysis_post` (Phase 09's vibe-heal wrappers), `review_self_review`, `review_address_comments` (Phase 09's harness wrappers), `unresolved_thread_count` (Phase 08), `manual_test_prompt` (Phase 10), `diff_stat` (files/lines-added/lines-removed vs the base ref, for Phase 10's `CompletionRecord` fields — a new git-diff primitive living here rather than in Phase 07's `git_ops`, which dropped `diff_stat` as unused by that phase; Phase 11's merge step is its only caller), `merge_gates_run` (Phase 06), `pr_merge` (Phase 08), `completion_log_append` (Phase 10). `build_live_toolchain(config) -> Toolchain` wires real implementations from the dependency phases' modules. `build_dry_run_toolchain() -> Toolchain` stubs every field (first `commit_all` call returns `True`, subsequent calls in the loop return `False`, simulating "agent implemented something, then address-comments converged with nothing left to commit"), enabling `--dry-run` to exercise the full branch/commit/scope/retry control flow against a disposable scratch repo with zero live external dependencies.

### 7.2 `run_phase` (`spec_prism_flow/build/phase_runner.py`)
`PhaseRunResult` frozen dataclass: `phase_number: int`, `merged: bool`, `completion_record: CompletionRecord`. `run_phase(clone: Path, config, phase: PhaseFile, *, toolchain: Toolchain | None = None, dry_run: bool = False, auto_merge: bool = True, resume: bool = False) -> PhaseRunResult`, executing, in order:
1. **Implement** — unless `resume` and Phase 08 reports an existing open PR for this phase: `checkout_fresh_branch` → `agent_run` (Phase 07, given the phase's rendered prompt) → `commit_all`; a `False`/empty result raises Phase 06's `EmptyImplementationError` immediately (no scope check, no push, on an empty diff). Then `diff_paths` against the base ref, followed by `scope_check` against `phase.scope` with that list.
2. **Push + PR** — `push_branch`, `fetch_resync`, `pr_create` (Phase 08), then persist the returned PR number/URL via Phase 08's resume-state save. (When resuming with an existing PR: skip steps 1–2 entirely, reuse the existing PR number/URL, proceed directly to step 3.)
3–6. **Iterate loop** — one shared `RetryBudget` (Phase 06) with `cycle_index` incrementing each pass; `RetryBudgetExhausted` raised once `cycle_index` exceeds `config.build.max_retry_cycles` *before* running that cycle's work. Each cycle: `fetch_resync` + `diff_paths` + `scope_check` (re-check, since address-comments below may modify files outside the original diff), static-analysis scan+conditional-post (Phase 09, only if `config.vibe_heal.enabled`), self-review+address-comments (Phase 09, only if `config.review.enabled`), `unresolved_thread_count` (Phase 08) — if `> 0`, loop again; else run `manual_test_prompt` (Phase 10, passing `--strict`'s effect through as a parameter) — a failed/skipped manual test under `--strict` raises Phase 06's `ManualTestFailed` instead of looping, a non-strict failure is recorded but does not block.
7. **Merge** — `diff_stat` against the base ref (for Phase 10's completion record), run `merge_gates_run` (Phase 06); if `auto_merge` is `False`, return without merging (`PhaseRunResult(merged=False)`).
8. **Record + merge** — `pr_merge` (Phase 08), then `completion_log_append` (Phase 10) with the finished record; on success, clear any resume-state file (Phase 08).
On any `OrchestrationError` raised at any step: best-effort call `completion_log_append` with whatever partial record exists (escalation reason + step reached), swallowing any exception from that best-effort write, then re-raise the original error unchanged.

## Acceptance criteria
- `Toolchain` has exactly the fields listed above; `build_live_toolchain` wires each to its real dependency-phase implementation.
- `build_dry_run_toolchain` simulates "implemented then converged" (`commit_all` returns `True` once, then `False`), letting `run_phase` complete end-to-end with zero live subprocess dependencies.
- `run_phase` executes the 8 steps in the documented order, raising `EmptyImplementationError` on an empty first-implementation diff before any scope check or push.
- The iterate loop's `RetryBudget` check happens before each cycle's work, using `config.build.max_retry_cycles`; the scope re-check runs every cycle, not just at step 1.
- `--strict`'s effect is threaded as a `run_phase`/`manual_test_prompt` parameter (not read from a global), and a failed/skipped manual test under `--strict` raises `ManualTestFailed` while non-strict records and proceeds.
- With `auto_merge=False`, `run_phase` stops after merge-gate checks and returns `PhaseRunResult(merged=False)` without calling `pr_merge`.
- On any `OrchestrationError`, a best-effort partial completion-log write occurs (swallowing its own failure) before the original error re-raises unchanged — never suppressed or replaced.
- `uv run pytest` passes for all three test files, including a dedicated escalations test asserting each `OrchestrationError` subclass is raised at its correct step; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_toolchain.py tests/build/test_phase_runner.py tests/build/test_phase_runner_escalations.py -v` and confirm all cases above pass.
- Run `run_phase` with `toolchain=build_dry_run_toolchain()` against a scratch git repo and confirm it completes end-to-end (through a mocked merge) with zero network/subprocess calls to `git`/`gh`/`claude`/`opencode`/`harness`/`vibe-heal` binaries.
- Force an empty implementation (dry-run toolchain returning no commit) and confirm `EmptyImplementationError` is raised before any push attempt.
- Force a retry-budget exhaustion (many iterate-loop cycles) and confirm `RetryBudgetExhausted` raises with the best-effort completion-log write still occurring first.
- Run once with `strict=True` and a failing manual test, and once with `strict=False` and the same failing manual test; confirm the former raises `ManualTestFailed` and the latter proceeds to merge-gate checks.
- Confirm no unhandled exceptions appear in any of the above beyond the intentionally-raised, named `OrchestrationError` subclasses.

## Depends on
- Phase 10 merged.
