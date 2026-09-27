## Scope
- harness/runners/common.py
- harness/runners/regret_review.py
- tests/runners/test_common.py
- tests/runners/test_common_comments.py
- tests/runners/test_regret_review.py

## Requirements
# Requirements: Regret Marker, Idempotency Helpers, and Comment Poster

## 1. Purpose / origin

This leaf is one part of `regret-review`'s decomposition tree (`requirements.md` §7.6, §10). It covers the pass's completion marker, its idempotency checks, and the function that renders confirmed regret findings into a GitHub PR comment body. It generalizes the existing marker convention (`INLINE_REVIEW_MARKER`, `DESIGN_REVIEW_MARKER`, `TRACEABILITY_REVIEW_MARKER`, `harness/runners/common.py:23-26`) to this fourth pass. It reuses the existing generic `has_pr_level_pass_comment`, `check_pr_level_pass_comment_status`, and `get_pr_level_flagged_locations` helpers instead of duplicating them.

## 2. Problem statement

Both runner-wiring leaves (§7.3, §7.4) need two things before they can call `common.run_pr_level_pass`. One is a way to recognize whether this PR already has a regret comment. The other is a way to build that comment's body once the backend-judgment leaf returns confirmed findings. Neither exists yet.

This leaf is built alongside chunk A-1-1 (config schema and docs), not after it. No code dependency exists between them. The project's build order still puts A-1-1 first. So this leaf's implementation session starts from a working tree that already has RegretReviewConfig merged.

## 3. Goals

- **G1.** The new `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"` constant is in `harness/runners/common.py`, alongside the other three markers.
- **G2.** `has_regret_review_comment(pr_number, repo, current_user, env)` and `check_regret_review_comment_status(pr_number, repo, current_user, env)` are in `common.py`. Each is a one-line call into the corresponding generic helper (`has_pr_level_pass_comment` or `check_pr_level_pass_comment_status`, `common.py:368-387`), parameterized with `REGRET_REVIEW_MARKER`. They mirror `has_design_review_comment` and `check_design_review_comment_status` (`common.py:394-404`) exactly.
- **G3.** A new `harness/runners/regret_review.py` module is the new home for everything regret-review-specific in this project (per `requirements.md` §10). It exports a `RegretFinding` data shape. It also exports a `build_regret_comment_body(findings: list[RegretFinding]) -> str` function. `RegretFinding` is a dataclass or a `TypedDict`. That is the implementer's choice. It must carry, at minimum, the current PR's file/line the finding concerns. It must carry the introducing PR number. It must carry the introducing comment's id, body, and author. It must carry a direct GitHub URL to that original comment.
- **G4.** `build_regret_comment_body` renders one Markdown section per finding, under a single `# Regret Review` heading. Each section shows the file/region, the introducing PR number + link, and a short quote of the original comment. The body ends with `REGRET_REVIEW_MARKER`. That matches `requirements.md` §7.6's comment-body description exactly. It is structurally similar to the body construction in `post_no_linked_ticket_comment` (`common.py:723-750`). This pass always has at least one finding when this function is called (see §9).

## 4. Non-goals

- This leaf does not call `gh pr comment` itself. That stays the wiring leaves' job, via `common.run_pr_level_pass`, which already owns backend invocation and comment-posting orchestration. This leaf only builds the *body string* and the *idempotency check*, not the post.
- This leaf does not define how `RegretFinding` instances are produced. That is the backend-judgment leaf's job (§7.2 steps 3–4). This leaf only defines the shape those instances must have to be renderable.
- This leaf does not deduplicate findings across reruns (§9 of `requirements.md`). A rerun after the marker is posted is a noop. This leaf's poster is never invoked a second time for the same PR in the same runner cycle.

## 5. Glossary

See `requirements.md` §5 for `Regret finding` and `Regret comment`. This leaf converts a list of `Regret finding`s into the literal body of the `Regret comment`.

## 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| `REGRET_REVIEW_MARKER` | How does a later run recognize a regret comment? | — | Constant string |
| `has_regret_review_comment` / `check_...` | Does this PR already have a regret comment? | PR number, repo, user, env | bool / bool\|None |
| `build_regret_comment_body` | What text goes in the comment? | `list[RegretFinding]` | Markdown string |

## 7. Detailed functional requirements

- The additions to `common.py` are two thin wrapper functions and one constant. They add no new logic. They follow the pattern that `has_design_review_comment` and `check_design_review_comment_status` already demonstrate: parameterize `has_pr_level_pass_comment` and `check_pr_level_pass_comment_status` with `REGRET_REVIEW_MARKER` instead of `DESIGN_REVIEW_MARKER`.
- `regret_review.py`'s `RegretFinding` must carry enough to build a direct comment URL.
- `scripts/pr-comments.py`'s cached comment dicts (fetched via `fetch_pr_comments`, `common.py:1223-1267`) already carry an `id`. The introducing PR number is known independently. A GitHub review/inline comment URL is constructible as `https://github.com/{repo}/pull/{pr_number}#discussion_r{id}` (inline/review-typed) or `.../pull/{pr_number}#issuecomment-{id}` (issue-typed). §7.2 step 1 already excludes issue-typed comments from ever becoming findings, so only the discussion-style URL form is needed here.
- `build_regret_comment_body` gives each finding section this exact Markdown, and prescribes nothing beyond it. Each section has three elements. The first is a `###` heading naming the file/region. The second is one line linking to the introducing PR (`#<number>`) and the constructed comment URL. The third is a blockquoted short excerpt of the original comment body. Truncate long bodies at a reasonable character cap, for example 300 characters, and note the truncation inline when applied.

## 8. Non-functional requirements

None exist beyond correctness. The body-builder is pure (no I/O), so it needs no timeout or external-call handling of its own. All I/O already happened by the time findings reach it.

## 9. Out-of-scope / explicit exclusions

- This leaf does not handle an empty `findings` list. Per `requirements.md` §7.2 step 4, the wiring leaves never call this poster when there are no confirmed findings. No backend invocation happens either. `build_regret_comment_body` may assume a non-empty list. It is not required to produce a "nothing to report" variant. The `has_design_review_comment` pass, by contrast, always posts something.

## 10. System/tool shape

- **Files touched:** `harness/runners/common.py` (edit — marker + two wrapper functions), `harness/runners/regret_review.py` (new — `RegretFinding`, `build_regret_comment_body`), `tests/runners/test_common.py` (edit — marker/wrapper tests), `tests/runners/test_regret_review.py` (new — body-builder tests), `tests/runners/test_common_comments.py` (edit — if any shared comment-fixture helper needs extending for regret-style fixtures).

## 11. Open decisions log

There are none. `requirements.md` §7.6 and §11 already resolve the marker name, the reuse of generic helpers, and the fact that the poster's output is a single PR-level comment.

## 12. Next step

Once this leaf is merged, the backend-judgment leaf (§7.2 steps 3–4) can produce real `RegretFinding` instances. Both runner-wiring leaves can then call `has_regret_review_comment` and `build_regret_comment_body` directly.

## Acceptance criteria
- The new `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"` constant is in `harness/runners/common.py`, alongside the other three markers.
- `has_regret_review_comment(pr_number, repo, current_user, env)` and `check_regret_review_comment_status(pr_number, repo, current_user, env)` are in `common.py`. Each is a one-line call into the corresponding generic helper (`has_pr_level_pass_comment` or `check_pr_level_pass_comment_status`, `common.py:368-387`), parameterized with `REGRET_REVIEW_MARKER`. They mirror `has_design_review_comment` and `check_design_review_comment_status` (`common.py:394-404`) exactly.
- A new `harness/runners/regret_review.py` module is the new home for everything regret-review-specific in this project (per `requirements.md` §10). It exports a `RegretFinding` data shape. It also exports a `build_regret_comment_body(findings: list[RegretFinding]) -> str` function. `RegretFinding` is a dataclass or a `TypedDict`. That is the implementer's choice. It must carry, at minimum, the current PR's file/line the finding concerns. It must carry the introducing PR number. It must carry the introducing comment's id, body, and author. It must carry a direct GitHub URL to that original comment.
- `build_regret_comment_body` renders one Markdown section per finding, under a single `# Regret Review` heading. Each section shows the file/region, the introducing PR number + link, and a short quote of the original comment. The body ends with `REGRET_REVIEW_MARKER`. That matches `requirements.md` §7.6's comment-body description exactly. It is structurally similar to the body construction in `post_no_linked_ticket_comment` (`common.py:723-750`). This pass always has at least one finding when this function is called (see §9).

## Manual test checklist
- Manually verify: The new `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"` constant is in `harness/runners/common.py`, alongside the other three markers.
- Manually verify: `has_regret_review_comment(pr_number, repo, current_user, env)` and `check_regret_review_comment_status(pr_number, repo, current_user, env)` are in `common.py`. Each is a one-line call into the corresponding generic helper (`has_pr_level_pass_comment` or `check_pr_level_pass_comment_status`, `common.py:368-387`), parameterized with `REGRET_REVIEW_MARKER`. They mirror `has_design_review_comment` and `check_design_review_comment_status` (`common.py:394-404`) exactly.
- Manually verify: A new `harness/runners/regret_review.py` module is the new home for everything regret-review-specific in this project (per `requirements.md` §10). It exports a `RegretFinding` data shape. It also exports a `build_regret_comment_body(findings: list[RegretFinding]) -> str` function. `RegretFinding` is a dataclass or a `TypedDict`. That is the implementer's choice. It must carry, at minimum, the current PR's file/line the finding concerns. It must carry the introducing PR number. It must carry the introducing comment's id, body, and author. It must carry a direct GitHub URL to that original comment.
- Manually verify: `build_regret_comment_body` renders one Markdown section per finding, under a single `# Regret Review` heading. Each section shows the file/region, the introducing PR number + link, and a short quote of the original comment. The body ends with `REGRET_REVIEW_MARKER`. That matches `requirements.md` §7.6's comment-body description exactly. It is structurally similar to the body construction in `post_no_linked_ticket_comment` (`common.py:723-750`). This pass always has at least one finding when this function is called (see §9).

## Depends on
- Phase 1 merged.
