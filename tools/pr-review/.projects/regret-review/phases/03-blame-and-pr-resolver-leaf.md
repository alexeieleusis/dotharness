## Scope
- harness/runners/regret_review.py
- harness/runners/common.py
- harness/config.py
- tests/runners/test_regret_review.py
- tests/conftest.py

## Requirements
# Requirements: Blame → Introducing Commit → Introducing PR Resolver

## 1. Purpose / origin

A leaf of `regret-review`'s decomposition tree (`requirements.md` §7.1, §10), covering
the pass's first mechanical stage: given the current PR's diff, produce the set of
unique introducing PRs whose review comments are worth checking. Builds on the
config leaf (reads `config.regret_review.max_diff_lines`/`authors`) and on existing
diff plumbing (`get_changed_files`/`get_file_diff`, `harness/runners/common.py:
1066-1098`).

## 2. Problem statement

Nothing in this codebase today turns "a PR's diff" into "the commit(s) that
introduced the lines being changed." `git blame` gives commit attribution but not a
PR; GitHub's commit→PR API gives a PR but needs a commit SHA first. Without this
leaf, the comment-matching leaf (§7.2) has no candidate introducing PRs/line ranges
to fetch comments for.

## 3. Goals

- **G1.** A bugfix-shaped gate: sum added+removed lines across every changed file in
  the current PR (identical counting rule to `build_file_review_section`'s diff-line
  count, `common.py:1117-1119`); if the total exceeds
  `config.regret_review.max_diff_lines`, return immediately with no blame/API calls.
- **G2.** Also gate on `config.regret_review.authors`: skip entirely if the current
  PR's author doesn't match (reuse `author_matches`, `common.py:895-898`).
- **G3.** For each changed file, for each contiguous removed/modified line range in
  the current PR's diff (added-only lines produce no range — nothing to blame), run
  `git blame -L <start>,<end> <base_ref> -- <path>` against the PR's base branch (or
  its merge-base with the current PR's head — implementer's choice, but must be the
  *pre-fix* state, never the fix's own commit) and collect the introducing commit SHA
  per range.
- **G4.** Deduplicate collected SHAs within one run — a commit blamed from multiple
  ranges/files is processed once.
- **G5.** For each unique SHA, call `gh api repos/{owner}/{repo}/commits/{sha}/pulls`
  to resolve introducing PR number(s). Zero results (API failure or a confirmed
  "no PR" answer) means silently skip that SHA's lines — no error above debug level.
  More than one PR result means evaluate every one of them downstream.
- **G6.** Return a structure carrying, per surviving `(file, line_range,
  introducing_sha, introducing_pr_number)` tuple, everything the comment-matching
  leaf (§7.2) needs to fetch that PR's comments and locate its own diff hunk for the
  blamed change.

## 4. Non-goals

- Not fetching the introducing PR's comments or matching them against anything —
  that's the next leaf (§7.2 steps 1–2) entirely.
- Not handling renamed/moved files beyond whatever `git blame`'s own default rename
  detection already does — no extra heuristic layered on (`requirements.md` §9).
- Not retrying a failed `gh api commits/{sha}/pulls` call within the same run — a
  rate-limited or transient failure is treated the same as "no PR found" (fail open,
  `requirements.md` §7.1 step 4, §8).

## 5. Glossary

See `requirements.md` §5 for `Introducing commit`/`Introducing PR`/`Bugfix-shaped
PR`. This leaf is the code that computes both of the former and applies the gate for
the latter.

## 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| Bugfix-shaped + author gate | Should this pass even run on this PR? | Current PR diff, config | bool (proceed / skip) |
| Blame resolver | Which commit introduced each removed/changed range? | Diff hunks, base ref | `(file, range, sha)` list, deduped |
| Commit→PR resolver | Which PR(s) merged that commit? | `sha` | `pr_number` list (possibly empty) |

## 7. Detailed functional requirements

- Implement as private functions in `harness/runners/regret_review.py` (per
  `requirements.md` §10 — this pass's plumbing lives in its own module, not
  `common.py`), e.g. `_is_bugfix_shaped`, `_blame_introducing_commits`,
  `_resolve_introducing_prs`, composed by one public entry point this leaf also
  defines, e.g. `find_introducing_prs(pr, config, wdir, env) -> list[IntroducingHunk]`
  (naming is implementer's choice; the shape must satisfy §7.2's needs).
- Diff-hunk parsing (to find which line ranges are removed/modified vs. purely added)
  can reuse `git diff --unified=0` output for the current PR against its base, parsed
  for `@@ -a,b +c,d @@` headers — `b` (the pre-image line count) being zero means a
  pure-addition hunk with nothing to blame.
- `git blame`/`gh api` calls go through the existing `run_cmd` subprocess wrapper
  (`common.py:75-107`) for consistent timeout/SIGTERM handling, using
  `config.regret_review.regret_review_timeout` as an overall budget the caller
  enforces across the whole per-PR sequence of calls (not a new per-call timeout
  constant — one shared budget, checked/decremented by the caller, is sufficient;
  implementer's choice on exact mechanics).

## 8. Non-functional requirements

- Bounded cost is structural: G1's gate means this leaf's own git/`gh` call count is
  proportional to `max_diff_lines` (default 50), never to whole-PR or whole-repo
  size.
- Every `gh api` call here is subject to the same rate limits as every existing
  `gh`-based helper (`TIMEOUT_GH = 30`, `common.py:16`) — no new rate-limit handling
  beyond the fail-open behavior in G5.

## 9. Out-of-scope / explicit exclusions

Not implemented here: anything past "list of introducing PRs + blamed locations."
Comment fetching, hunk matching, and backend judgment are separate leaves.

## 10. System/tool shape

- **Files touched:** `harness/runners/regret_review.py` (new — blame/resolver
  functions), `harness/runners/common.py` (edit — only if a small, genuinely
  general-purpose helper is worth adding here, e.g. exposing the diff-line-counting
  logic `build_file_review_section` already has as a standalone function so this
  leaf doesn't duplicate it), `harness/config.py` (no edit — read-only dependency on
  `RegretReviewConfig`, already landed by the config leaf), `tests/runners/
  test_regret_review.py` (new — fixture-based tests for blame parsing and commit→PR
  resolution, mocking `run_cmd`/`gh api` rather than hitting real GitHub, per
  `requirements.md` §10's testing note), `tests/conftest.py` (edit — a shared fixture
  for a fake local git repo with a known blame history, if useful across this and the
  next leaf's tests).

## 11. Open decisions log

None carried from `requirements.md` — its §11 already resolves the fail-open/
silent-skip behavior (item 4) this leaf implements.

## 12. Next step

Once merged, the comment-fetch-and-hunk-match leaf (§7.2 steps 1–2) consumes this
leaf's `IntroducingHunk`-shaped output directly.

## Acceptance criteria
- A bugfix-shaped gate: sum added+removed lines across every changed file in the current PR (identical counting rule to `build_file_review_section`'s diff-line count, `common.py:1117-1119`); if the total exceeds `config.regret_review.max_diff_lines`, return immediately with no blame/API calls.
- Also gate on `config.regret_review.authors`: skip entirely if the current PR's author doesn't match (reuse `author_matches`, `common.py:895-898`).
- For each changed file, for each contiguous removed/modified line range in the current PR's diff (added-only lines produce no range — nothing to blame), run `git blame -L <start>,<end> <base_ref> -- <path>` against the PR's base branch (or its merge-base with the current PR's head — implementer's choice, but must be the *pre-fix* state, never the fix's own commit) and collect the introducing commit SHA per range.
- Deduplicate collected SHAs within one run — a commit blamed from multiple ranges/files is processed once.
- For each unique SHA, call `gh api repos/{owner}/{repo}/commits/{sha}/pulls` to resolve introducing PR number(s). Zero results (API failure or a confirmed "no PR" answer) means silently skip that SHA's lines — no error above debug level. More than one PR result means evaluate every one of them downstream.
- Return a structure carrying, per surviving `(file, line_range, introducing_sha, introducing_pr_number)` tuple, everything the comment-matching leaf (§7.2) needs to fetch that PR's comments and locate its own diff hunk for the blamed change.

## Manual test checklist
- Manually verify: A bugfix-shaped gate: sum added+removed lines across every changed file in the current PR (identical counting rule to `build_file_review_section`'s diff-line count, `common.py:1117-1119`); if the total exceeds `config.regret_review.max_diff_lines`, return immediately with no blame/API calls.
- Manually verify: Also gate on `config.regret_review.authors`: skip entirely if the current PR's author doesn't match (reuse `author_matches`, `common.py:895-898`).
- Manually verify: For each changed file, for each contiguous removed/modified line range in the current PR's diff (added-only lines produce no range — nothing to blame), run `git blame -L <start>,<end> <base_ref> -- <path>` against the PR's base branch (or its merge-base with the current PR's head — implementer's choice, but must be the *pre-fix* state, never the fix's own commit) and collect the introducing commit SHA per range.
- Manually verify: Deduplicate collected SHAs within one run — a commit blamed from multiple ranges/files is processed once.
- Manually verify: For each unique SHA, call `gh api repos/{owner}/{repo}/commits/{sha}/pulls` to resolve introducing PR number(s). Zero results (API failure or a confirmed "no PR" answer) means silently skip that SHA's lines — no error above debug level. More than one PR result means evaluate every one of them downstream.
- Manually verify: Return a structure carrying, per surviving `(file, line_range, introducing_sha, introducing_pr_number)` tuple, everything the comment-matching leaf (§7.2) needs to fetch that PR's comments and locate its own diff hunk for the blamed change.

## Depends on
- Phase 2 merged.
