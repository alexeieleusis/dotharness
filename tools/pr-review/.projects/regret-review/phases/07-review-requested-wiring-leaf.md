## Scope
- harness/runners/review_requested.py
- tests/runners/test_review_requested.py
- docs/commands/review-requested.md
- docs/commands/index.md
- README.md

## Requirements
# Requirements: `review-requested` Wiring

## 1. Purpose / origin

This is the final leaf of `regret-review`'s decomposition tree (`requirements.md` §7.4, §10).
It covers the second of two runner integrations. Its reference implementation is
`_run_design_review` in this runner (`harness/runners/review_requested.py:320-388`),
which this leaf copies exactly. It is a direct sibling of chunk A-3-1, the
just-merged `self-review` wiring leaf (§7.3). It shares the core mechanics of
that leaf, wires a different runner, and adds no persisted state.

## 2. Problem statement

`self-review` wiring (previous leaf) only covers PRs the current user authored.
`review-requested` covers PRs where GitHub review was explicitly requested from the
harness's own account. That is a distinct population of PRs. `requirements.md` §11
item 1 already resolved that both runners are in scope. Without this leaf, the
`review-requested` runner never gives a PR a regret-review pass.

## 3. Goals

- **G1.** `review_requested.py` gains a new `_run_regret_review(...)` function. It
  runs alongside the existing `design_ok`/`traceability_ok` calls
  (`review_requested.py:216-221`), gated on `config.regret_review.enabled` the same
  way as the `self-review` leaf.
- **G2.** This leaf adds no persisted state. That matches design/traceability review
  in this runner exactly (`review_requested.py:332-335`). Since `review_requested`
  has no state file at all, `has_regret_review_comment` is this pass's only
  "already done" signal. It checks live via `gh` on every invocation.
- **G3.** This leaf reuses the same `review-regret.md` instructions file as the
  `self-review` leaf. It also reuses the same core-mechanics functions:
  `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt
  builder/parser, and `build_regret_comment_body`. It adds no new logic to any of
  them. It adds only a second call site.
- **G4.** This leaf reuses `common.run_pr_level_pass` unchanged.

## 4. Non-goals

- This leaf does not duplicate any of the core-mechanics functions for this runner.
  They are runner-agnostic by construction. They take `pr`/`config`/`wdir`/`env`,
  and none of them depends on `self_review.py`'s internal state.
- This leaf adds no persisted state to `review_requested.py`. That runner is
  deliberately stateless. The existing comment for design/traceability review
  already explains why (`review_requested.py:332-335`). This leaf does not change
  that.

## 5. Glossary

See `requirements.md` §5. This leaf is integration. It adds no new domain
vocabulary.

## 6. Feature/component breakdown

This leaf covers a single feature (the second runner's wiring). No table is needed.

## 7. Detailed functional requirements

- `run(config, pr_url=None)` (`review_requested.py:46-...`) gains a
  `regret_done = has_regret_review_comment(pr_number, config.repo.name, current_user,
  env)` computation. It sits alongside the existing `design_done`/`traceability_done`
  lines (`review_requested.py:79-80`). The function also gains a
  `regret_ok = _run_regret_review(...)` call alongside the existing
  `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`). The call is
  gated on `config.regret_review.enabled`, the same way the `self-review` leaf gates
  its own call.
- `_run_regret_review`'s signature mirrors `_run_design_review`'s in this file
  (`review_requested.py:320-388`): `pr, config, knowledge_dir, extra_knowledge,
  backend, wdir, env, current_user, regret_done, ctx`. The function reads
  `review-regret.md` from `knowledge_dir` (`knowledge_dir / "review-regret.md"`).
  That matches the existing `knowledge_dir / "review-design.md"` read at
  `review_requested.py:357`.
- `has_comment_fn` passed into `run_pr_level_pass` here is
  `has_regret_review_comment(pr_number, repo_name, current_user, env)` directly.
  This leaf does not wrap it in a `check_...` tri-state variant. That matches what
  the design and traceability review passes already do in this runner. `self_review.py`
  is the contrast: it uses the tri-state `check_design_review_comment_status` because
  it has persisted state to protect from an inconclusive check. This runner has no
  persisted state. So the simpler bool form is correct here, per
  `run_pr_level_pass`'s own docstring (`common.py:770-777`).

## 8. Non-functional requirements

None beyond what `run_pr_level_pass` and the composed core-mechanics leaves already
provide.

## 9. Out-of-scope / explicit exclusions

This leaf adds no new CLI flag or command. The rationale is the same as the
`self-review` leaf's §9.

## 10. System/tool shape

- **Files touched:** `harness/runners/review_requested.py` (edit), `tests/runners/
  test_review_requested.py` (edit). The new tests mirror the existing design-review
  wiring tests in this file: the enabled/disabled gate, idempotency via the marker
  check, and the no-state-mutation assertion.

## 11. Open decisions log

None. `requirements.md` §7.4/§11 item 1 already resolves that this pass is part of
`review-requested`.

## 12. Next step

Once merged, every leaf of this project's decomposition is complete. `plan review`
becomes the exit gate before `build run` starts opening PRs for each phase in
sequence.

## Acceptance criteria
- `review_requested.py` gains a new `_run_regret_review(...)` function. It runs alongside the existing `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`), gated on `config.regret_review.enabled` the same way as the `self-review` leaf.
- This leaf adds no persisted state. That matches design/traceability review in this runner exactly (`review_requested.py:332-335`). Since `review_requested` has no state file at all, `has_regret_review_comment` is this pass's only "already done" signal. It checks live via `gh` on every invocation.
- This leaf reuses the same `review-regret.md` instructions file as the `self-review` leaf. It also reuses the same core-mechanics functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. It adds no new logic to any of them. It adds only a second call site.
- This leaf reuses `common.run_pr_level_pass` unchanged.

## Manual test checklist
- Manually verify: `review_requested.py` gains a new `_run_regret_review(...)` function. It runs alongside the existing `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`), gated on `config.regret_review.enabled` the same way as the `self-review` leaf.
- Manually verify: This leaf adds no persisted state. That matches design/traceability review in this runner exactly (`review_requested.py:332-335`). Since `review_requested` has no state file at all, `has_regret_review_comment` is this pass's only "already done" signal. It checks live via `gh` on every invocation.
- Manually verify: This leaf reuses the same `review-regret.md` instructions file as the `self-review` leaf. It also reuses the same core-mechanics functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. It adds no new logic to any of them. It adds only a second call site.
- Manually verify: This leaf reuses `common.run_pr_level_pass` unchanged.

## Depends on
- Phase 6 merged.
