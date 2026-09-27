## Scope
- harness/runners/regret_review.py
- harness/runners/common.py
- scripts/pr-comments.py
- tests/runners/test_regret_review.py
- tests/runners/test_common_comments.py

## Requirements
# Requirements: Comment Fetch and Diff-Hunk Matching

## 1. Purpose / origin

A leaf of `regret-review`'s decomposition tree (`requirements.md` §7.2 steps 1–2,
§10), covering the stage between "we know the introducing PR" (previous leaf, §7.1)
and "we know what the backend should judge" (next leaf, §7.2 steps 3–4): fetch each
introducing PR's comments and keep only the ones that plausibly concern the blamed
change.

## 2. Problem statement

The previous leaf produces introducing PRs and blamed line ranges; on its own that's
not yet useful, because most of an introducing PR's comments have nothing to do with
the specific lines this current PR's fix touches. Without hunk-tolerant matching
(`requirements.md` §7.2 step 2, §11 item 2), either every comment on the file would
be a false-positive candidate (exact-line matching would instead miss real regrets
lost to drift). This leaf is what makes the candidate set both complete and precise
enough to hand to a backend for judgment.

## 3. Goals

- **G1.** For each introducing PR surfaced by the blame/resolver leaf, fetch its
  comments via the existing `fetch_pr_comments` (`common.py:1223-1267`), pointed at
  the introducing PR's number instead of the current PR's.
- **G2.** Keep only `inline`/`review`-typed comments (the only types carrying
  `path`/`line`, per `fetch_pr_comments`'s own docstring); `issue`-typed comments are
  never candidates.
- **G3.** For each surviving comment, compute the diff hunk the introducing commit's
  own change occupied (diff the introducing commit against its parent, restricted to
  the blamed file, taking the `@@` line range), and treat the comment as a candidate
  if its `path` matches and its `line` falls within that hunk's range — not only the
  exact blamed line (`requirements.md` §7.2 step 2, glossary "Diff hunk").
- **G4.** Return the full set of candidate `(comment, blamed_change)` pairs across
  every file/hunk in the current PR, ready to be batched into one prompt by the next
  leaf. An empty result (no blame hits, or blame hits with no matching comment) is a
  valid, expected outcome, not an error.

## 4. Non-goals

- Not deciding whether a candidate is a genuine regret — that judgment is the
  backend-judgment leaf's job (§7.2 steps 3–4). This leaf only narrows "every comment
  on the introducing PR" down to "comments plausibly about the blamed change."
- Not handling file renames beyond whatever the blame leaf already resolved (this
  leaf receives a fixed `path` per blamed change from that leaf and matches comments
  against exactly that path — it does not itself re-derive rename history).
- Not caching comment fetches across multiple runs — each `regret-review` invocation
  fetches fresh (mirrors every existing PR-level pass; `fetch_pr_comments`'s own
  `scripts/pr-comments.py` caching is per-invocation, not persisted across runs).

## 5. Glossary

See `requirements.md` §5 for `Diff hunk`. This leaf is the concrete implementation of
"same diff hunk" matching that definition describes.

## 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| Comment fetch (per introducing PR) | What did reviewers say on that PR? | Introducing PR number | Raw `inline`/`review`/`issue` comment list |
| Type filter | Which comments even have a location? | Raw comment list | `inline`/`review` only |
| Hunk-window computation | What line range counts as "the same change"? | Introducing commit SHA, file | `(start, end)` range |
| Match | Does this comment fall in that range? | Comment `(path, line)`, hunk range | Candidate pair, or discarded |

## 7. Detailed functional requirements

- Add a public function to `harness/runners/regret_review.py`, e.g.
  `find_regret_candidates(introducing_hunks, config, wdir, env) ->
  list[RegretCandidate]`, consuming the previous leaf's output directly.
- One `fetch_pr_comments` call per unique introducing PR number across the whole
  current-PR run (not per blamed hunk) — if two blamed hunks resolve to the same
  introducing PR, fetch its comments once and match against both hunks locally.
- The hunk-window computation (diffing the introducing commit against its parent for
  one file) is a second, small git subprocess call per unique introducing commit —
  reuse `run_cmd` (`common.py:75-107`) the same way the blame leaf does, and the same
  shared per-PR timeout budget (`config.regret_review.regret_review_timeout`).
- `RegretCandidate` (naming implementer's choice) must carry enough for both the next
  leaf (comment body/author/diff context for the prompt) and the poster leaf's
  `RegretFinding` shape (comment id, introducing PR number, file/region) — define it
  as whatever intermediate shape makes both leaves' inputs easy to construct from it,
  documented at the point of definition.

## 8. Non-functional requirements

- Cost stays bounded by the previous leaf's gate: at most one comment fetch and one
  hunk-diff call per unique introducing PR/commit, never per blamed line
  individually.
- Same rate-limit/fail-open posture as the blame leaf (§8 there) — a fetch or
  hunk-diff failure for one introducing PR causes that PR's candidates to be silently
  dropped, not the whole run to fail.

## 9. Out-of-scope / explicit exclusions

Not building any new caching layer for `fetch_pr_comments` beyond what
`scripts/pr-comments.py` already provides — this leaf is a pure consumer of that
existing mechanism.

## 10. System/tool shape

- **Files touched:** `harness/runners/regret_review.py` (edit — adds
  `find_regret_candidates` and its helpers), `harness/runners/common.py` (no
  edit — pure consumer of `fetch_pr_comments`), `scripts/pr-comments.py` (edit, only
  if fetching an arbitrary PR's comments surfaces a gap not already covered by its
  existing `--pr` argument — verify first before assuming a change is needed),
  `tests/runners/test_regret_review.py` (edit — hunk-matching fixture tests),
  `tests/runners/test_common_comments.py` (edit — if a shared comment-fixture helper
  needs a regret-specific variant).

## 11. Open decisions log

None carried from `requirements.md` — §11 item 2 already resolves the hunk-tolerance
matching model this leaf implements.

## 12. Next step

Once merged, the backend-judgment leaf (§7.2 steps 3–4) consumes this leaf's
candidate-pair list directly, batching them into a single per-PR prompt.

## Acceptance criteria
- For each introducing PR surfaced by the blame/resolver leaf, fetch its comments via the existing `fetch_pr_comments` (`common.py:1223-1267`), pointed at the introducing PR's number instead of the current PR's.
- Keep only `inline`/`review`-typed comments (the only types carrying `path`/`line`, per `fetch_pr_comments`'s own docstring); `issue`-typed comments are never candidates.
- For each surviving comment, compute the diff hunk the introducing commit's own change occupied (diff the introducing commit against its parent, restricted to the blamed file, taking the `@@` line range), and treat the comment as a candidate if its `path` matches and its `line` falls within that hunk's range — not only the exact blamed line (`requirements.md` §7.2 step 2, glossary "Diff hunk").
- Return the full set of candidate `(comment, blamed_change)` pairs across every file/hunk in the current PR, ready to be batched into one prompt by the next leaf. An empty result (no blame hits, or blame hits with no matching comment) is a valid, expected outcome, not an error.

## Manual test checklist
- Manually verify: For each introducing PR surfaced by the blame/resolver leaf, fetch its comments via the existing `fetch_pr_comments` (`common.py:1223-1267`), pointed at the introducing PR's number instead of the current PR's.
- Manually verify: Keep only `inline`/`review`-typed comments (the only types carrying `path`/`line`, per `fetch_pr_comments`'s own docstring); `issue`-typed comments are never candidates.
- Manually verify: For each surviving comment, compute the diff hunk the introducing commit's own change occupied (diff the introducing commit against its parent, restricted to the blamed file, taking the `@@` line range), and treat the comment as a candidate if its `path` matches and its `line` falls within that hunk's range — not only the exact blamed line (`requirements.md` §7.2 step 2, glossary "Diff hunk").
- Manually verify: Return the full set of candidate `(comment, blamed_change)` pairs across every file/hunk in the current PR, ready to be batched into one prompt by the next leaf. An empty result (no blame hits, or blame hits with no matching comment) is a valid, expected outcome, not an error.

## Depends on
- Phase 3 merged.
