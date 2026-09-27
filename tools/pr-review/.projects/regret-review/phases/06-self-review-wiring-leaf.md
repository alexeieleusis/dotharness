## Scope
- harness/runners/self_review.py
- harness/state.py
- tests/runners/test_self_review.py
- tests/test_state.py
- docs/commands/self-review.md

## Requirements
# Requirements: `self-review` Wiring

## 1. Purpose / origin

This file is a leaf of `regret-review`'s decomposition tree (`requirements.md` §7.3, §10). It covers the first of two runner integrations. The new function follows the shape of `_run_design_review` (`harness/runners/self_review.py:221-259`), the reference implementation.

## 2. Problem statement

Each earlier leaf in this project (config, marker/poster, blame/resolver, comment/hunk-match, backend-judgment) produces a working pipeline in isolation. Nothing in `self_review.py` calls any of them yet. Without this leaf, `regret-review` never runs against a user's open PRs.

## 3. Goals

- **G1.** Add a new `_run_regret_review(...)` function to `self_review.py`. Call it from `_run_locked`'s per-PR loop (`self_review.py:508-519`), alongside the existing `run_design`/`run_traceability` calls, but only when `config.regret_review.enabled` is `true`. When `config.regret_review.enabled` is `false`, `_run_locked` never calls it for any PR. The design and traceability reviews have no such gate.
- **G2.** Add new persisted state, `regret_reviewed_prs`, to `harness/state.py`, alongside `design_reviewed_prs` and `traceability_reviewed_prs`. Give `regret_reviewed_prs` its own read, write, and prune helpers (for example, `get_regret_reviewed_prs` and `add_regret_reviewed_pr`). Mirror the existing shape of `add_design_reviewed_pr` and `prune_self_review_state` (`harness/state.py:183-224`). Track the state independently. This pass's retry must never force, or be forced by, any other pass's retry.
- **G3.** In `_run_regret_review`, call the earlier leaves' functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. Then call `common.run_pr_level_pass` (`common.py:752-816`) for the noop-check → backend-run → marker-verify orchestration. Leave `common.run_pr_level_pass` unchanged.
- **G4.** When a run succeeds, call `state.add_regret_reviewed_pr(config.repo_slug, number)`. Judge success from `run_pr_level_pass`'s return value. This mirrors the `if result: state.add_design_reviewed_pr(...)` line in `_run_design_review` (`self_review.py:257-258`).

## 4. Non-goals

- Do not implement `review-requested` wiring. That is the sibling leaf (§7.4).
- Do not change `_run_locked`'s existing `run_design`/`run_traceability` computation or their state. This leaf only adds a third, independently-gated pass alongside them.
- Do not add a new orchestration skeleton. This leaf reuses `common.run_pr_level_pass` unchanged, per `requirements.md` §7.3.

## 5. Glossary

See `requirements.md` §5. This leaf is pure integration. It adds no new domain vocabulary.

## 6. Feature/component breakdown

This leaf has a single feature (one runner's wiring). This section needs no table.

## 7. Detailed functional requirements

- Add a `run_regret = config.regret_review.enabled and number not in regret_reviewed` computation to `_run_locked` (`self_review.py:484-...`), alongside its existing `run_design`/`run_traceability` lines. Add an `if run_regret: _run_regret_review(...)` call alongside the existing `if run_design:`/`if run_traceability:` blocks (`self_review.py:470-477`). Read `regret_reviewed` from `pruned_state["regret_reviewed_prs"]`, mirroring `design_reviewed`/`traceability_reviewed` (`self_review.py:495-496`).
- The signature and body of `_run_regret_review` should mirror `_run_design_review`'s parameter list (design/traceability instructions, pr, number, config, ctx, backend, wdir, current_user, env). Match the list as closely as this pass's data allows. Substitute the regret-specific instructions file (`review-regret.md`) and the candidate-finding calls for the design-review-specific prompt builder.
- Set `is_done` for this pass to `has_regret_review_comment(number, config.repo.name, current_user, env)`, from the marker/poster leaf. Check it before any blame or comment-fetch work. On a rerun, a PR that already has a regret comment makes zero git or `gh` calls for this pass. This matches the early-skip behavior of every other pass.
- Include `regret_reviewed_prs` in `prune_self_review_state`'s pruning logic (`state.py:183-...`), the same way `design_reviewed_prs` is included. This keeps a closed PR's entry from lingering forever.

## 8. Non-functional requirements

This leaf adds no non-functional requirements beyond what `run_pr_level_pass` already provides. `run_pr_level_pass` provides a backend timeout via `harness.backend_timeout_seconds`. This leaf's git and `gh` calls are bounded by `regret_review_timeout`. The earlier leaf functions enforce that timeout. This leaf does not re-implement the timeout enforcement.

## 9. Out-of-scope / explicit exclusions

Do not build a new CLI flag or command for this pass. `[regret_review].enabled` alone decides whether the pass runs. This matches every other pass in `self-review`. Each of those runs only when the runner is invoked.

## 10. System/tool shape

- **Files touched:** `harness/runners/self_review.py` (edit), `harness/state.py` (edit — new `regret_reviewed_prs` state key + helpers), `tests/runners/test_self_review.py` (edit — new tests mirroring the existing design-review wiring tests: enabled/disabled gate, idempotency, state persistence), `tests/test_state.py` (edit — `regret_reviewed_prs` read, write, and prune tests).

## 11. Open decisions log

None — `requirements.md` §7.3/§11 item 1 already resolves that this pass is part of `self-review`.

## 12. Next step

After this leaf merges, the sibling `review-requested` wiring leaf (§7.4) comes next. It reuses the same core-mechanics leaves and adds no persisted state (that runner has none).

## Acceptance criteria
- A new `_run_regret_review(...)` function exists in `self_review.py`. The per-PR loop in `_run_locked` calls it (`self_review.py:508-519`), alongside the existing `run_design`/`run_traceability` calls. The function runs only when `config.regret_review.enabled` is `true`. When `config.regret_review.enabled` is `false`, the loop never calls the function for any PR. The design and traceability reviews have no such gate.
- `regret_reviewed_prs` exists in `harness/state.py`, alongside `design_reviewed_prs` and `traceability_reviewed_prs`. It has its own read, write, and prune helpers (for example, `get_regret_reviewed_prs` and `add_regret_reviewed_pr`), mirroring the existing shape of `add_design_reviewed_pr` and `prune_self_review_state` (`harness/state.py:183-224`). The state is tracked independently. This pass's retry must never force, or be forced by, any other pass's retry.
- `_run_regret_review` calls the earlier leaves' functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. It calls `common.run_pr_level_pass` (`common.py:752-816`) for the noop-check → backend-run → marker-verify orchestration. `common.run_pr_level_pass` is unchanged.
- On a successful run, the pass calls `state.add_regret_reviewed_pr(config.repo_slug, number)`. Success is judged from `run_pr_level_pass`'s return value. This mirrors the `if result: state.add_design_reviewed_pr(...)` line in `_run_design_review` (`self_review.py:257-258`).

## Manual test checklist
- Manually verify: A new `_run_regret_review(...)` function exists in `self_review.py`. The per-PR loop in `_run_locked` calls it (`self_review.py:508-519`), alongside the existing `run_design`/`run_traceability` calls. The function runs only when `config.regret_review.enabled` is `true`. When `config.regret_review.enabled` is `false`, the loop never calls the function for any PR. The design and traceability reviews have no such gate.
- Manually verify: `regret_reviewed_prs` exists in `harness/state.py`, alongside `design_reviewed_prs` and `traceability_reviewed_prs`. It has its own read, write, and prune helpers (for example, `get_regret_reviewed_prs` and `add_regret_reviewed_pr`), mirroring the existing shape of `add_design_reviewed_pr` and `prune_self_review_state` (`harness/state.py:183-224`). The state is tracked independently. This pass's retry must never force, or be forced by, any other pass's retry.
- Manually verify: `_run_regret_review` calls the earlier leaves' functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. It calls `common.run_pr_level_pass` (`common.py:752-816`) for the noop-check → backend-run → marker-verify orchestration. `common.run_pr_level_pass` is unchanged.
- Manually verify: On a successful run, the pass calls `state.add_regret_reviewed_pr(config.repo_slug, number)`. Success is judged from `run_pr_level_pass`'s return value. This mirrors the `if result: state.add_design_reviewed_pr(...)` line in `_run_design_review` (`self_review.py:257-258`).

## Depends on
- Phase 5 merged.
