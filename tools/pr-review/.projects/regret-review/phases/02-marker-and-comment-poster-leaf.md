## Scope
- harness/runners/common.py
- harness/runners/regret_review.py
- tests/runners/test_common.py
- tests/runners/test_common_comments.py
- tests/runners/test_regret_review.py

## Requirements
# Requirements: Regret Marker, Idempotency Helpers, and Comment Poster

## 1. Purpose / origin

A leaf of `regret-review`'s decomposition tree (`requirements.md` §7.6, §10), covering
the pass's completion marker, its idempotency checks, and the function that renders
confirmed regret findings into an actual GitHub PR comment body. It generalizes the
existing marker convention (`INLINE_REVIEW_MARKER` / `DESIGN_REVIEW_MARKER` /
`TRACEABILITY_REVIEW_MARKER`, `harness/runners/common.py:23-26`) to this fourth pass,
and reuses the already-generic `has_pr_level_pass_comment`/
`check_pr_level_pass_comment_status`/`get_pr_level_flagged_locations` helpers rather
than duplicating them.

## 2. Problem statement

Both runner-wiring leaves (§7.3, §7.4) need, before they can call
`common.run_pr_level_pass`, a way to (a) recognize whether this PR already has a
regret comment, and (b) build that comment's body once the backend-judgment leaf has
returned confirmed findings. Neither exists yet. Built alongside chunk A-1-1 (config schema and docs) rather than after it -- no code dependency exists between them -- but this project's sequential build order still lands A-1-1 first, so this leaf's own implementation session starts from a working tree that already has RegretReviewConfig merged in.

## 3. Goals

- **G1.** A new `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"` constant in
  `harness/runners/common.py`, alongside the other three markers.
- **G2.** `has_regret_review_comment(pr_number, repo, current_user, env)` and
  `check_regret_review_comment_status(pr_number, repo, current_user, env)` in
  `common.py`, each a one-line call into the existing generic
  `has_pr_level_pass_comment`/`check_pr_level_pass_comment_status`
  (`common.py:368-387`) parameterized with `REGRET_REVIEW_MARKER` — mirroring
  `has_design_review_comment`/`check_design_review_comment_status`
  (`common.py:394-404`) exactly.
- **G3.** A new `harness/runners/regret_review.py` module (this project's new
  home for everything regret-review-specific, per `requirements.md` §10) exporting a
  `RegretFinding` data shape (dataclass or `TypedDict` — implementer's choice, but
  it must carry at minimum: the current PR's file/line the finding concerns, the
  introducing PR number, the introducing comment's id/body/author, and a direct
  GitHub URL to that original comment) and a `build_regret_comment_body(findings:
  list[RegretFinding]) -> str` function.
- **G4.** `build_regret_comment_body` renders one Markdown section per finding (file/
  region, introducing PR number + link, a short quote of the original comment) under a
  single `# Regret Review` heading, ending with `REGRET_REVIEW_MARKER` — matching
  `requirements.md` §7.6's comment-body description exactly, and structurally similar
  to `post_no_linked_ticket_comment`'s body-construction style
  (`common.py:723-750`) though this pass always has at least one finding when this
  function is called at all (see §9).

## 4. Non-goals

- Not calling `gh pr comment` itself — that stays the wiring leaves' job (via
  `common.run_pr_level_pass`, which already owns backend invocation +
  comment-posting orchestration). This leaf only builds the *body string* and the
  *idempotency check*, not the post.
- Not defining how `RegretFinding` instances get produced — that's the
  backend-judgment leaf's job (§7.2 steps 3–4). This leaf only defines the shape
  those instances must have to be renderable.
- Not deduplicating findings across reruns (§9 of `requirements.md`: a rerun after
  the marker is posted is a full noop; this leaf's poster is never invoked a second
  time for the same PR in the same runner cycle).

## 5. Glossary

See `requirements.md` §5 for `Regret finding`/`Regret comment`. This leaf is what
turns a list of the former into the literal body of the latter.

## 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| `REGRET_REVIEW_MARKER` | How is a regret comment recognized on a later run? | — | Constant string |
| `has_regret_review_comment` / `check_...` | Has this PR already gotten a regret comment? | PR number, repo, user, env | bool / bool\|None |
| `build_regret_comment_body` | What text goes in the comment? | `list[RegretFinding]` | Markdown string |

## 7. Detailed functional requirements

- `common.py` additions are two thin wrapper functions plus one constant — no new
  logic beyond what `has_design_review_comment`/`check_design_review_comment_status`
  already demonstrate as the pattern (parameterize `has_pr_level_pass_comment`/
  `check_pr_level_pass_comment_status` with `REGRET_REVIEW_MARKER` instead of
  `DESIGN_REVIEW_MARKER`).
- `regret_review.py`'s `RegretFinding` must carry enough to build a direct comment
  URL. `scripts/pr-comments.py`'s cached comment dicts (fetched via
  `fetch_pr_comments`, `common.py:1223-1267`) already carry an `id` and the
  introducing PR's number is known independently — a GitHub review/inline comment
  URL is constructible as `https://github.com/{repo}/pull/{pr_number}#discussion_r{id}`
  (inline/review-typed) or `.../pull/{pr_number}#issuecomment-{id}` (issue-typed,
  though §7.2 step 1 already excludes issue-typed comments from ever becoming
  findings, so only the discussion-style URL form is actually needed here).
- `build_regret_comment_body`'s per-finding section format (exact Markdown, not
  prescribed further than): a `###` heading naming the file/region, one line linking
  to the introducing PR (`#<number>`) and the constructed comment URL, and a
  blockquoted short excerpt of the original comment body (truncate long bodies —
  pick a reasonable character cap, e.g. 300 characters, and note the truncation
  inline if applied).

## 8. Non-functional requirements

None beyond correctness: the body-builder is pure (no I/O), so it needs no timeout or
external-call handling of its own — all I/O already happened by the time findings
reach it.

## 9. Out-of-scope / explicit exclusions

- Not handling an empty `findings` list — per `requirements.md` §7.2 step 4, when
  there are no confirmed findings, the wiring leaves never call this poster at all
  (no backend invocation happens either). `build_regret_comment_body` may assume a
  non-empty list and is not required to produce a "nothing to report" variant, unlike
  `has_design_review_comment`'s pass which always posts something.

## 10. System/tool shape

- **Files touched:** `harness/runners/common.py` (edit — marker + two wrapper
  functions), `harness/runners/regret_review.py` (new — `RegretFinding`,
  `build_regret_comment_body`), `tests/runners/test_common.py` (edit — marker/
  wrapper tests), `tests/runners/test_regret_review.py` (new — body-builder tests),
  `tests/runners/test_common_comments.py` (edit — if any shared comment-fixture
  helper needs extending for regret-style fixtures).

## 11. Open decisions log

None — `requirements.md` §7.6/§11 already resolves the marker name, the reuse of
generic helpers, and the poster's single-PR-level-comment shape.

## 12. Next step

Once merged, the backend-judgment leaf (§7.2 steps 3–4) can produce real
`RegretFinding` instances, and both runner-wiring leaves can call
`has_regret_review_comment`/`build_regret_comment_body` directly.

## Acceptance criteria
- A new `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"` constant in `harness/runners/common.py`, alongside the other three markers.
- `has_regret_review_comment(pr_number, repo, current_user, env)` and `check_regret_review_comment_status(pr_number, repo, current_user, env)` in `common.py`, each a one-line call into the existing generic `has_pr_level_pass_comment`/`check_pr_level_pass_comment_status` (`common.py:368-387`) parameterized with `REGRET_REVIEW_MARKER` — mirroring `has_design_review_comment`/`check_design_review_comment_status` (`common.py:394-404`) exactly.
- A new `harness/runners/regret_review.py` module (this project's new home for everything regret-review-specific, per `requirements.md` §10) exporting a `RegretFinding` data shape (dataclass or `TypedDict` — implementer's choice, but it must carry at minimum: the current PR's file/line the finding concerns, the introducing PR number, the introducing comment's id/body/author, and a direct GitHub URL to that original comment) and a `build_regret_comment_body(findings: list[RegretFinding]) -> str` function.
- `build_regret_comment_body` renders one Markdown section per finding (file/ region, introducing PR number + link, a short quote of the original comment) under a single `# Regret Review` heading, ending with `REGRET_REVIEW_MARKER` — matching `requirements.md` §7.6's comment-body description exactly, and structurally similar to `post_no_linked_ticket_comment`'s body-construction style (`common.py:723-750`) though this pass always has at least one finding when this function is called at all (see §9).

## Manual test checklist
- Manually verify: A new `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"` constant in `harness/runners/common.py`, alongside the other three markers.
- Manually verify: `has_regret_review_comment(pr_number, repo, current_user, env)` and `check_regret_review_comment_status(pr_number, repo, current_user, env)` in `common.py`, each a one-line call into the existing generic `has_pr_level_pass_comment`/`check_pr_level_pass_comment_status` (`common.py:368-387`) parameterized with `REGRET_REVIEW_MARKER` — mirroring `has_design_review_comment`/`check_design_review_comment_status` (`common.py:394-404`) exactly.
- Manually verify: A new `harness/runners/regret_review.py` module (this project's new home for everything regret-review-specific, per `requirements.md` §10) exporting a `RegretFinding` data shape (dataclass or `TypedDict` — implementer's choice, but it must carry at minimum: the current PR's file/line the finding concerns, the introducing PR number, the introducing comment's id/body/author, and a direct GitHub URL to that original comment) and a `build_regret_comment_body(findings: list[RegretFinding]) -> str` function.
- Manually verify: `build_regret_comment_body` renders one Markdown section per finding (file/ region, introducing PR number + link, a short quote of the original comment) under a single `# Regret Review` heading, ending with `REGRET_REVIEW_MARKER` — matching `requirements.md` §7.6's comment-body description exactly, and structurally similar to `post_no_linked_ticket_comment`'s body-construction style (`common.py:723-750`) though this pass always has at least one finding when this function is called at all (see §9).

## Depends on
- Phase 1 merged.
