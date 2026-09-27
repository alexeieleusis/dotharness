## Scope
- harness/runners/review_requested.py
- tests/runners/test_review_requested.py
- docs/commands/review-requested.md
- docs/commands/index.md
- README.md

## Requirements
# Requirements: `review-requested` Wiring

## 1. Purpose / origin

The final leaf of `regret-review`'s decomposition tree (`requirements.md` §7.4, §10),
covering the second of two runner integrations. Follows `_run_design_review`'s exact
shape in this runner (`harness/runners/review_requested.py:320-388`) as its reference
implementation, and is a direct sibling of chunk A-3-1 (the just-merged `self-review` wiring leaf, §7.3) -- same core mechanics, different runner, no persisted state.

## 2. Problem statement

`self-review` wiring (previous leaf) only covers PRs the current user authored.
`review-requested` covers PRs where GitHub review was explicitly requested from the
harness's own account — a distinct population of PRs (`requirements.md` §11 item 1
already resolved that both runners are in scope). Without this leaf, a PR reviewed
via `review-requested` never gets a regret-review pass at all.

## 3. Goals

- **G1.** A new `_run_regret_review(...)` function in `review_requested.py`, called
  alongside the existing `design_ok`/`traceability_ok` calls
  (`review_requested.py:216-221`), gated the same way as the `self-review` leaf on
  `config.regret_review.enabled`.
- **G2.** No persisted state — exactly like design/traceability review in this runner
  (`review_requested.py:332-335`): `has_regret_review_comment` (checked live via `gh`
  on every invocation) is this pass's only "already done" signal here, since
  `review_requested` has no state file at all.
- **G3.** Same `review-regret.md` instructions file and the same core-mechanics
  functions (`find_introducing_prs`, `find_regret_candidates`, the backend-judgment
  prompt builder/parser, `build_regret_comment_body`) as the `self-review` leaf —
  this leaf adds no new logic to any of those, only a second call site.
- **G4.** Same `common.run_pr_level_pass` reuse, unchanged.

## 4. Non-goals

- Not duplicating any of the core-mechanics functions for this runner — they are
  runner-agnostic by construction (they take `pr`/`config`/`wdir`/`env`, not anything
  specific to `self_review.py`'s internal state).
- Not adding persisted state to `review_requested.py` — that runner's design is
  deliberately stateless (`review_requested.py:332-335`'s existing comment for
  design/traceability review already explains why), and this leaf doesn't change
  that.

## 5. Glossary

See `requirements.md` §5. This leaf is purely integration — no new domain
vocabulary.

## 6. Feature/component breakdown

Single feature (the second runner's wiring) — no table needed.

## 7. Detailed functional requirements

- `run(config, pr_url=None)` (`review_requested.py:46-...`) gains a
  `regret_done = has_regret_review_comment(pr_number, config.repo.name, current_user,
  env)` computation alongside its existing `design_done`/`traceability_done` lines
  (`review_requested.py:79-80`), and a `regret_ok = _run_regret_review(...)` call
  alongside the existing `design_ok`/`traceability_ok` calls
  (`review_requested.py:216-221`), gated on `config.regret_review.enabled` the same
  way the `self-review` leaf gates its own call.
- `_run_regret_review`'s signature mirrors `_run_design_review`'s in this file
  (`review_requested.py:320-388`): `pr, config, knowledge_dir, extra_knowledge,
  backend, wdir, env, current_user, regret_done, ctx`, reading `review-regret.md`
  from `knowledge_dir` (`knowledge_dir / "review-regret.md"`, matching the existing
  `knowledge_dir / "review-design.md"` read at `review_requested.py:357`).
- `has_comment_fn` passed into `run_pr_level_pass` here is
  `has_regret_review_comment(pr_number, repo_name, current_user, env)` directly — no
  wrapping in a `check_...` tri-state variant, matching how design/traceability
  review already do it in this runner (contrast with `self_review.py`, which uses
  the tri-state `check_design_review_comment_status` because it has persisted state
  to protect from an inconclusive check; this runner has none, so the simpler bool
  form is correct here, per `run_pr_level_pass`'s own docstring, `common.py:770-777`).

## 8. Non-functional requirements

None beyond what `run_pr_level_pass` and the composed core-mechanics leaves already
provide.

## 9. Out-of-scope / explicit exclusions

Not adding any new CLI flag or command — same rationale as the `self-review` leaf's
§9.

## 10. System/tool shape

- **Files touched:** `harness/runners/review_requested.py` (edit), `tests/runners/
  test_review_requested.py` (edit — new tests mirroring the existing design-review
  wiring tests in this file: enabled/disabled gate, idempotency via the marker check,
  no-state-mutation assertion).

## 11. Open decisions log

None — `requirements.md` §7.4/§11 item 1 already resolves that this pass hooks into
`review-requested`.

## 12. Next step

Once merged, every leaf of this project's decomposition is complete. `plan review`
becomes the exit gate before `build run` starts opening PRs for each phase in
sequence.

## Acceptance criteria
- A new `_run_regret_review(...)` function in `review_requested.py`, called alongside the existing `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`), gated the same way as the `self-review` leaf on `config.regret_review.enabled`.
- No persisted state — exactly like design/traceability review in this runner (`review_requested.py:332-335`): `has_regret_review_comment` (checked live via `gh` on every invocation) is this pass's only "already done" signal here, since `review_requested` has no state file at all.
- Same `review-regret.md` instructions file and the same core-mechanics functions (`find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, `build_regret_comment_body`) as the `self-review` leaf — this leaf adds no new logic to any of those, only a second call site.
- Same `common.run_pr_level_pass` reuse, unchanged.

## Manual test checklist
- Manually verify: A new `_run_regret_review(...)` function in `review_requested.py`, called alongside the existing `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`), gated the same way as the `self-review` leaf on `config.regret_review.enabled`.
- Manually verify: No persisted state — exactly like design/traceability review in this runner (`review_requested.py:332-335`): `has_regret_review_comment` (checked live via `gh` on every invocation) is this pass's only "already done" signal here, since `review_requested` has no state file at all.
- Manually verify: Same `review-regret.md` instructions file and the same core-mechanics functions (`find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, `build_regret_comment_body`) as the `self-review` leaf — this leaf adds no new logic to any of those, only a second call site.
- Manually verify: Same `common.run_pr_level_pass` reuse, unchanged.

## Depends on
- Phase 6 merged.
