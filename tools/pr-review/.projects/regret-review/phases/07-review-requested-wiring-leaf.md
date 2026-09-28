## Scope
- harness/runners/review_requested.py
- tests/runners/test_review_requested.py
- docs/commands/review-requested.md

## Requirements
# Requirements: `review-requested` Wiring

### 1. Purpose / origin

This is the final leaf of `regret-review`'s decomposition tree (`requirements.md` §7.4, §10).
It covers the second of two runner integrations. Its reference implementation is
`_run_design_review` in this runner (`harness/runners/review_requested.py:320-388`),
which this leaf copies exactly. It is a direct sibling of chunk A-3-1, the
just-merged `self-review` wiring leaf (§7.3). It shares the core mechanics of
that leaf, wires a different runner, and adds no persisted state.

### 2. Problem statement

`self-review` wiring (previous leaf) only covers PRs the current user authored.
`review-requested` covers PRs where GitHub review was explicitly requested from the
harness's own account. That is a distinct population of PRs. `requirements.md` §11
item 1 already resolved that both runners are in scope. Without this leaf, the
`review-requested` runner never gives a PR a regret-review pass.

### 3. Goals

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
- **G5.** `docs/commands/review-requested.md` is updated to document the regret
  pass in every place this runner's behavior changes: the intro names it as an
  optional fifth PR-level pass, the "What it does" steps cover the gated
  `review-regret.md` prompt read, the extended skip gate, the pass itself, and
  the extended reviewer-removal gate, and the Configuration, State and
  idempotency, and Notes sections gain their regret-specific items.
  `docs/commands/index.md` and `README.md` need no change: phase 01 already
  gave them accurate regret-review mentions.

### 4. Non-goals

- This leaf does not duplicate any of the core-mechanics functions for this runner.
  They are runner-agnostic by construction. They take `pr`/`config`/`wdir`/`env`,
  and none of them depends on `self_review.py`'s internal state.
- This leaf adds no persisted state to `review_requested.py`. That runner is
  deliberately stateless. The existing comment for design/traceability review
  already explains why (`review_requested.py:332-335`). This leaf does not change
  that.

### 5. Glossary

See `requirements.md` §5. This leaf is integration. It adds no new domain
vocabulary.

### 6. Feature/component breakdown

This leaf covers a single feature (the second runner's wiring). No table is needed.

### 7. Detailed functional requirements

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
- `docs/commands/review-requested.md` documents the pass wherever this runner's
  behavior changes:
  - The intro paragraph names the regret review as an optional fifth PR-level
    pass that runs on top of the existing four when `[regret_review].enabled`
    is `true` (off by default).
  - "What it does" step 4 states that `.../review-regret.md` is read
    separately, only when `[regret_review].enabled` is `true` and the regret
    pass actually runs for a PR.
  - "What it does" step 6 extends the "all the outstanding parts are
    already done" skip check to include the regret pass's marker (when
    enabled), adds a **Regret pass (optional)** bullet — candidate search,
    at most one backend invocation per PR, the `<!-- dotharness-review-regret -->`
    marker as this pass's *only* idempotency signal in this stateless
    runner, the comment posted by the runner itself because the backend
    replies with verdict lines only, and a posting-nothing run re-running
    with a fresh candidate search — and extends the reviewer-removal gate to
    include the regret pass (when enabled), with a failed regret comment post
    counting as a failure.
  - The Configuration table's `harness.knowledge_dir` row lists
    `pr-review/review-regret.md` (only when enabled), and a new paragraph
    states that this runner consults the `[regret_review]` fields only when
    `regret_review.enabled` is `true` and that the off pass costs nothing.
  - "State and idempotency" gains a regret-marker item (independent of
    (1)-(4), posted only when the backend confirms at least one finding) and
    the reviewer-removal item is renumbered and extended to include the
    regret pass (when enabled).
  - The Notes add `regret_ok` to the reviewer-removal expression (when
    enabled) and a cost bullet: gated, at most one backend invocation per PR,
    no persisted state so a posting-nothing run re-runs the bounded candidate
    search, bounded by `[regret_review].max_diff_lines` and
    `[regret_review].regret_review_timeout`.

### 8. Non-functional requirements

None beyond what `run_pr_level_pass` and the composed core-mechanics leaves already
provide.

### 9. Out-of-scope / explicit exclusions

This leaf adds no new CLI flag or command. The rationale is the same as the
`self-review` leaf's §9.

`docs/commands/index.md` and `README.md` are not touched by this leaf. Both
already carry accurate regret-review mentions from the phase-01 leaf, and
nothing in this leaf changes what either of them states.

### 10. System/tool shape

- **Files touched:** `harness/runners/review_requested.py` (edit), `tests/runners/
  test_review_requested.py` (edit), `docs/commands/review-requested.md` (edit —
  regret-pass documentation per G5 and the §7 docs requirement). The new tests
  mirror the existing design-review wiring tests in this file: the enabled/
  disabled gate, idempotency via the marker check, and the no-state-mutation
  assertion.

### 11. Open decisions log

None. `requirements.md` §7.4/§11 item 1 already resolves that this pass is part of
`review-requested`.

### 12. Next step

Once merged, every leaf of this project's decomposition is complete. `plan review`
becomes the exit gate before `build run` starts opening PRs for each phase in
sequence.

## Acceptance criteria
- `review_requested.py` gains a new `_run_regret_review(...)` function. It runs alongside the existing `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`), gated on `config.regret_review.enabled` the same way as the `self-review` leaf.
- This leaf adds no persisted state. That matches design/traceability review in this runner exactly (`review_requested.py:332-335`). Since `review_requested` has no state file at all, `has_regret_review_comment` is this pass's only "already done" signal. It checks live via `gh` on every invocation.
- This leaf reuses the same `review-regret.md` instructions file as the `self-review` leaf. It also reuses the same core-mechanics functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. It adds no new logic to any of them. It adds only a second call site.
- This leaf reuses `common.run_pr_level_pass` unchanged.
- `docs/commands/review-requested.md` documents the regret pass in every place this runner's behavior changes: the intro names it as an optional fifth PR-level pass gated on `[regret_review].enabled` (off by default); "What it does" covers the gated `review-regret.md` read, the extended skip gate, the pass's own bullet (marker-only idempotency, runner-posted comment, verdict-lines-only backend, re-run on posting nothing), and the extended reviewer-removal gate; the Configuration table and a new note cover `pr-review/review-regret.md` and the off-by-default cost; "State and idempotency" gains the regret-marker item and renumbers the reviewer-removal item; and the Notes add `regret_ok` and a cost bullet. `docs/commands/index.md` and `README.md` are unchanged.

## Manual test checklist
- Manually verify: `review_requested.py` gains a new `_run_regret_review(...)` function. It runs alongside the existing `design_ok`/`traceability_ok` calls (`review_requested.py:216-221`), gated on `config.regret_review.enabled` the same way as the `self-review` leaf.
- Manually verify: This leaf adds no persisted state. That matches design/traceability review in this runner exactly (`review_requested.py:332-335`). Since `review_requested` has no state file at all, `has_regret_review_comment` is this pass's only "already done" signal. It checks live via `gh` on every invocation.
- Manually verify: This leaf reuses the same `review-regret.md` instructions file as the `self-review` leaf. It also reuses the same core-mechanics functions: `find_introducing_prs`, `find_regret_candidates`, the backend-judgment prompt builder/parser, and `build_regret_comment_body`. It adds no new logic to any of them. It adds only a second call site.
- Manually verify: This leaf reuses `common.run_pr_level_pass` unchanged.
- Manually verify: `docs/commands/review-requested.md` documents the regret pass in every place this runner's behavior changes: the intro names it as an optional fifth PR-level pass gated on `[regret_review].enabled` (off by default); "What it does" covers the gated `review-regret.md` read, the extended skip gate, the pass's own bullet (marker-only idempotency, runner-posted comment, verdict-lines-only backend, re-run on posting nothing), and the extended reviewer-removal gate; the Configuration table and a new note cover `pr-review/review-regret.md` and the off-by-default cost; "State and idempotency" gains the regret-marker item and renumbers the reviewer-removal item; and the Notes add `regret_ok` and a cost bullet. `docs/commands/index.md` and `README.md` are unchanged.

## Depends on
- Phase 6 merged.
