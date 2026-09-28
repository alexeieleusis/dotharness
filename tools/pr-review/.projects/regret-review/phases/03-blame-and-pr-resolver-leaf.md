## Scope
- harness/runners/regret_review.py
- harness/runners/common.py
- harness/config.py
- tests/runners/test_regret_review.py
- tests/conftest.py

## Requirements
# Requirements: Blame → Introducing Commit → Introducing PR Resolver

### 1. Purpose / origin

This leaf is part of `regret-review`'s decomposition tree (`requirements.md` §7.1, §10). It covers the first mechanical stage of the pass. Given the current PR's diff, it produces the set of unique introducing PRs whose review comments are worth checking. It reads the config leaf (`config.regret_review.max_diff_lines`/`authors`) and reuses the existing diff plumbing (`get_changed_files`/`get_file_diff` in `harness/runners/common.py`).

### 2. Problem statement

Nothing in this codebase today turns a PR's diff into the commit(s) that introduced the lines being changed. `git blame` gives commit attribution but not a PR. GitHub's commit→PR API gives a PR but needs a commit SHA first. Without this leaf, the comment-matching leaf (§7.2) has no candidate introducing PRs or line ranges to fetch comments for.

### 3. Goals

- **G1.** Gate the pass on bugfix shape. Sum the added and removed lines across every changed file in the current PR. Use the same counting rule as `build_file_review_section`'s diff-line count (`common.py`). If the total exceeds `config.regret_review.max_diff_lines`, return immediately. Make no blame or API calls.
- **G2.** Also gate on `config.regret_review.authors`. Skip the PR entirely if its author does not match. Reuse `author_matches` (`common.py`).
- **G3.** For each changed file, find each contiguous removed or modified line range in the current PR's diff. Added-only lines produce no range, because there is nothing to blame. For each such range, run `git blame -L <start>,<end> <base_ref> -- <path>` against the PR's base branch. Use the base branch, or its merge-base with the current PR's head. The choice is the implementer's, but it must be the *pre-fix* state, never the fix's own commit. Collect the introducing commit SHA for each range.
- **G4.** Deduplicate the collected SHAs within one run. A commit blamed from multiple ranges or files is processed once.
- **G5.** For each unique SHA, call `gh api repos/{owner}/{repo}/commits/{sha}/pulls` to resolve the introducing PR number(s). Zero results — from an API failure or a confirmed no-PR answer — mean silently skipping that SHA's lines. Log no error above debug level. More than one PR result means evaluating every one of them downstream.
- **G6.** Return a structure. For each surviving `(file, line_range, introducing_sha, introducing_pr_number)` tuple, it carries everything the comment-matching leaf (§7.2) needs to fetch that PR's comments and to locate its own diff hunk for the blamed change.

### 4. Non-goals

- Do not fetch the introducing PR's comments or match them against anything. That is the next leaf (§7.2 steps 1–2) entirely.
- Do not handle renamed or moved files beyond what `git blame`'s own default rename detection already does. Add no extra heuristic (`requirements.md` §9).
- Do not retry a failed `gh api commits/{sha}/pulls` call within the same run. A rate-limited or transient failure is treated the same as no PR found (fail open, `requirements.md` §7.1 step 4, §8).

### 5. Glossary

See `requirements.md` §5 for `Introducing commit`, `Introducing PR`, and `Bugfix-shaped PR`. This leaf computes the `Introducing commit` and the `Introducing PR`. It applies the gate for the `Bugfix-shaped PR`.

### 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| Bugfix-shaped + author gate | Should this pass even run on this PR? | Current PR diff, config | bool (proceed / skip) |
| Blame resolver | Which commit introduced each removed/changed range? | Diff hunks, base ref | `(file, range, sha)` list, deduped |
| Commit→PR resolver | Which PR(s) merged that commit? | `sha` | `pr_number` list (possibly empty) |

### 7. Detailed functional requirements

- Implement the leaf as private functions in `harness/runners/regret_review.py`. Per `requirements.md` §10, this pass's plumbing lives in its own module, not `common.py`. Example private functions are `_is_bugfix_shaped`, `_blame_introducing_commits`, and `_resolve_introducing_prs`. One public entry point composes them. This leaf also defines that entry point, for example `find_introducing_prs(pr, config, wdir, env) -> list[IntroducingHunk]`. The naming is the implementer's choice. The shape must satisfy §7.2's needs.
- To find which line ranges are removed or modified versus purely added, parse the diff hunks. Reuse the `git diff --unified=0` output for the current PR against its base. Read the `@@ -a,b +c,d @@` headers. When `b` (the pre-image line count) is zero, the hunk is a pure addition with nothing to blame.
- All `git blame` and `gh api` calls use the existing `run_cmd` subprocess wrapper (`common.py`). This gives consistent timeout and SIGTERM handling. Use `config.regret_review.regret_review_timeout` as one overall budget. The caller enforces that budget across the whole per-PR sequence of calls. Do not add a new per-call timeout constant. One shared budget, checked and decremented by the caller, is sufficient. The exact mechanics are the implementer's choice.

### 8. Non-functional requirements

- Bounded cost is structural. G1's gate keeps this leaf's own git/`gh` call count proportional to `max_diff_lines` (default 50). It is never proportional to whole-PR or whole-repo size.
- Every `gh api` call here is subject to the same rate limits as every existing `gh`-based helper (`TIMEOUT_GH = 30`, `common.py`). Add no new rate-limit handling beyond the fail-open behavior in G5.

### 9. Out-of-scope / explicit exclusions

Not implemented here is anything past the list of introducing PRs and blamed locations. Comment fetching, hunk matching, and backend judgment are separate leaves.

### 10. System/tool shape

- **Files touched:** `harness/runners/regret_review.py` (new — the blame and resolver functions). `harness/runners/common.py` (edit — only if a small, genuinely general-purpose helper is worth adding here, for example exposing the logic that counts diff lines, which `build_file_review_section` already has, as a standalone function so this leaf does not duplicate it). `harness/config.py` (no edit — a read-only dependency on `RegretReviewConfig`, already landed by the config leaf). `tests/runners/test_regret_review.py` (new — fixture-based tests for blame parsing and commit→PR resolution. Mock `run_cmd` and `gh api` rather than hitting real GitHub, per `requirements.md` §10's testing note). `tests/conftest.py` (edit — a shared fixture for a fake local git repo with a known blame history, if useful across this and the next leaf's tests).

### 11. Open decisions log

None carried from `requirements.md`. Its §11 already resolves the fail-open, silent-skip behavior (item 4) that this leaf implements.

### 12. Next step

Once merged, the leaf that fetches comments and matches hunks (§7.2 steps 1–2) consumes this leaf's `IntroducingHunk`-shaped output directly.

## Acceptance criteria
- Gate the pass on bugfix shape. Sum the added and removed lines across every changed file in the current PR. Use the same counting rule as `build_file_review_section`'s diff-line count (`common.py`). If the total exceeds `config.regret_review.max_diff_lines`, return immediately. Make no blame or API calls.
- Also gate on `config.regret_review.authors`. Skip the PR entirely if its author does not match. Reuse `author_matches` (`common.py`).
- For each changed file, find each contiguous removed or modified line range in the current PR's diff. Added-only lines produce no range, because there is nothing to blame. For each such range, run `git blame -L <start>,<end> <base_ref> -- <path>` against the PR's base branch. Use the base branch, or its merge-base with the current PR's head. The choice is the implementer's, but it must be the *pre-fix* state, never the fix's own commit. Collect the introducing commit SHA for each range.
- Deduplicate the collected SHAs within one run. A commit blamed from multiple ranges or files is processed once.
- For each unique SHA, call `gh api repos/{owner}/{repo}/commits/{sha}/pulls` to resolve the introducing PR number(s). Zero results — from an API failure or a confirmed no-PR answer — mean silently skipping that SHA's lines. Log no error above debug level. More than one PR result means evaluating every one of them downstream.
- Return a structure. For each surviving `(file, line_range, introducing_sha, introducing_pr_number)` tuple, it carries everything the comment-matching leaf (§7.2) needs to fetch that PR's comments and to locate its own diff hunk for the blamed change.

## Manual test checklist
- Manually verify: the pass gates on bugfix shape. Sum the added and removed lines across every changed file in the current PR. Use the same counting rule as `build_file_review_section`'s diff-line count (`common.py`). If the total exceeds `config.regret_review.max_diff_lines`, return immediately. Make no blame or API calls.
- Manually verify: also gate on `config.regret_review.authors`. Skip the PR entirely if its author does not match. Reuse `author_matches` (`common.py`).
- Manually verify: for each changed file, find each contiguous removed or modified line range in the current PR's diff. Added-only lines produce no range, because there is nothing to blame. For each such range, run `git blame -L <start>,<end> <base_ref> -- <path>` against the PR's base branch. Use the base branch, or its merge-base with the current PR's head. The choice is the implementer's, but it must be the *pre-fix* state, never the fix's own commit. Collect the introducing commit SHA for each range.
- Manually verify: deduplicate the collected SHAs within one run. A commit blamed from multiple ranges or files is processed once.
- Manually verify: for each unique SHA, call `gh api repos/{owner}/{repo}/commits/{sha}/pulls` to resolve the introducing PR number(s). Zero results — from an API failure or a confirmed no-PR answer — mean silently skipping that SHA's lines. Log no error above debug level. More than one PR result means evaluating every one of them downstream.
- Manually verify: return a structure. For each surviving `(file, line_range, introducing_sha, introducing_pr_number)` tuple, it carries everything the comment-matching leaf (§7.2) needs to fetch that PR's comments and to locate its own diff hunk for the blamed change.

## Depends on
- Phase 2 merged.
