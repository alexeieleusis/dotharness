## Scope
- harness/runners/self_review.py
- harness/state.py
- tests/runners/test_self_review.py
- tests/test_state.py
- docs/commands/self-review.md

## Requirements
# Requirements: `self-review` Wiring

## 1. Purpose / origin

A leaf of `regret-review`'s decomposition tree (`requirements.md` §7.3, §10), covering
the first of two runner integrations. Follows `_run_design_review`'s exact shape
(`harness/runners/self_review.py:221-259`) as its reference implementation.

## 2. Problem statement

Every earlier leaf in this project (config, marker/poster, blame/resolver, comment/
hunk-match, backend-judgment) produces a working pipeline in isolation, but nothing
in `self_review.py` calls any of it yet. Without this leaf, `regret-review` never
actually runs against a user's own open PRs.

## 3. Goals

- **G1.** A new `_run_regret_review(...)` function in `self_review.py`, called from
  `_run_locked`'s per-PR loop (`self_review.py:508-519`) alongside the existing
  `run_design`/`run_traceability` calls — but only when `config.regret_review.enabled`
  is `true`; when `false`, this pass is never invoked at all for any PR, not merely
  skipped per-PR (contrast with design/traceability review, which have no such gate).
- **G2.** New persisted state, `regret_reviewed_prs`, added to `harness/state.py`
  alongside `design_reviewed_prs`/`traceability_reviewed_prs` — its own
  read/write/prune helpers (e.g. `get_regret_reviewed_prs`, `add_regret_reviewed_pr`),
  mirroring `add_design_reviewed_pr`/`prune_self_review_state`'s existing shape
  (`harness/state.py:183-224`) exactly, tracked independently so this pass's retry
  never forces, or is forced by, any other pass's retry behavior.
- **G3.** `_run_regret_review` composes the earlier leaves' functions
  (`find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt
  builder/parser, `build_regret_comment_body`) and calls
  `common.run_pr_level_pass` (`common.py:752-816`) for the actual noop-check →
  backend-run → marker-verify orchestration, unchanged.
- **G4.** On a successful run (per `run_pr_level_pass`'s return value), call
  `state.add_regret_reviewed_pr(config.repo_slug, number)`, mirroring
  `_run_design_review`'s own `if result: state.add_design_reviewed_pr(...)` line
  (`self_review.py:257-258`).

## 4. Non-goals

- Not implementing `review-requested` wiring — that's the sibling leaf (§7.4).
- Not changing `_run_locked`'s existing `run_design`/`run_traceability` computation
  or their state — this leaf only adds a third, independently-gated pass alongside
  them.
- Not adding a new orchestration skeleton — `common.run_pr_level_pass` is reused
  unchanged, per `requirements.md` §7.3.

## 5. Glossary

See `requirements.md` §5. This leaf is purely integration — no new domain vocabulary.

## 6. Feature/component breakdown

Single feature (one runner's wiring) — no table needed.

## 7. Detailed functional requirements

- `_run_locked` (`self_review.py:484-...`) gains a `run_regret =
  config.regret_review.enabled and number not in regret_reviewed` computation
  alongside its existing `run_design`/`run_traceability` lines, and a
  `if run_regret: _run_regret_review(...)` call alongside the existing
  `if run_design:`/`if run_traceability:` blocks (`self_review.py:470-477`).
  `regret_reviewed` itself comes from `pruned_state["regret_reviewed_prs"]`
  (mirroring `design_reviewed`/`traceability_reviewed`, `self_review.py:495-496`).
- `_run_regret_review`'s signature and body should mirror `_run_design_review`'s
  parameter list (design/traceability instructions, pr, number, config, ctx, backend,
  wdir, current_user, env) as closely as the actual data this pass needs allows —
  substituting the regret-specific instructions file (`review-regret.md`) and
  candidate-finding calls for the design-review-specific prompt builder.
- `is_done` for this pass is `has_regret_review_comment(number, config.repo.name,
  current_user, env)` (from the marker/poster leaf), checked before doing any
  blame/comment-fetch work at all — an already-regret-commented PR does zero git/`gh`
  calls for this pass on a rerun, matching every other pass's upfront-skip
  optimization.
- `regret_reviewed_prs` must be included in `prune_self_review_state`'s pruning logic
  (`state.py:183-...`) the same way `design_reviewed_prs` already is, so a closed PR's
  entry doesn't linger forever.

## 8. Non-functional requirements

None beyond what `run_pr_level_pass` already provides (backend timeout via
`harness.backend_timeout_seconds`); this leaf's own git/`gh` calls are bounded by
`regret_review_timeout`, enforced inside the composed leaf functions this leaf calls,
not re-implemented here.

## 9. Out-of-scope / explicit exclusions

Not building any new CLI flag or command for this pass specifically — it runs (or
doesn't) purely as a function of `[regret_review].enabled`, exactly like every other
pass in `self-review` runs as a function of the runner being invoked at all.

## 10. System/tool shape

- **Files touched:** `harness/runners/self_review.py` (edit), `harness/state.py`
  (edit — new `regret_reviewed_prs` state key + helpers), `tests/runners/
  test_self_review.py` (edit — new tests mirroring the existing design-review
  wiring tests: enabled/disabled gate, idempotency, state persistence), `tests/
  test_state.py` (edit — `regret_reviewed_prs` read/write/prune tests).

## 11. Open decisions log

None — `requirements.md` §7.3/§11 item 1 already resolves that this pass hooks into
`self-review`.

## 12. Next step

Once merged, the sibling `review-requested` wiring leaf (§7.4) lands next, reusing
the same core-mechanics leaves but with no persisted state (that runner has none).

## Acceptance criteria
- A new `_run_regret_review(...)` function in `self_review.py`, called from `_run_locked`'s per-PR loop (`self_review.py:508-519`) alongside the existing `run_design`/`run_traceability` calls — but only when `config.regret_review.enabled` is `true`; when `false`, this pass is never invoked at all for any PR, not merely skipped per-PR (contrast with design/traceability review, which have no such gate).
- New persisted state, `regret_reviewed_prs`, added to `harness/state.py` alongside `design_reviewed_prs`/`traceability_reviewed_prs` — its own read/write/prune helpers (e.g. `get_regret_reviewed_prs`, `add_regret_reviewed_pr`), mirroring `add_design_reviewed_pr`/`prune_self_review_state`'s existing shape (`harness/state.py:183-224`) exactly, tracked independently so this pass's retry never forces, or is forced by, any other pass's retry behavior.
- `_run_regret_review` composes the earlier leaves' functions (`find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, `build_regret_comment_body`) and calls `common.run_pr_level_pass` (`common.py:752-816`) for the actual noop-check → backend-run → marker-verify orchestration, unchanged.
- On a successful run (per `run_pr_level_pass`'s return value), call `state.add_regret_reviewed_pr(config.repo_slug, number)`, mirroring `_run_design_review`'s own `if result: state.add_design_reviewed_pr(...)` line (`self_review.py:257-258`).

## Manual test checklist
- Manually verify: A new `_run_regret_review(...)` function in `self_review.py`, called from `_run_locked`'s per-PR loop (`self_review.py:508-519`) alongside the existing `run_design`/`run_traceability` calls — but only when `config.regret_review.enabled` is `true`; when `false`, this pass is never invoked at all for any PR, not merely skipped per-PR (contrast with design/traceability review, which have no such gate).
- Manually verify: New persisted state, `regret_reviewed_prs`, added to `harness/state.py` alongside `design_reviewed_prs`/`traceability_reviewed_prs` — its own read/write/prune helpers (e.g. `get_regret_reviewed_prs`, `add_regret_reviewed_pr`), mirroring `add_design_reviewed_pr`/`prune_self_review_state`'s existing shape (`harness/state.py:183-224`) exactly, tracked independently so this pass's retry never forces, or is forced by, any other pass's retry behavior.
- Manually verify: `_run_regret_review` composes the earlier leaves' functions (`find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, `build_regret_comment_body`) and calls `common.run_pr_level_pass` (`common.py:752-816`) for the actual noop-check → backend-run → marker-verify orchestration, unchanged.
- Manually verify: On a successful run (per `run_pr_level_pass`'s return value), call `state.add_regret_reviewed_pr(config.repo_slug, number)`, mirroring `_run_design_review`'s own `if result: state.add_design_reviewed_pr(...)` line (`self_review.py:257-258`).

## Depends on
- Phase 5 merged.
