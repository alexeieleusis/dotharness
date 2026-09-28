# regret-review — Overview

## 1. Problem statement

When a bug is fixed, the code being corrected had often been reviewed before. Sometimes a reviewer had flagged the exact issue in a comment that was never addressed. That "regret" is invisible today. Nothing connects a bugfix PR to the review comment on the original PR that introduced the bug. So the same kind of oversight, from the same author or a different one, tends to recur unnoticed.

`regret-review` is a new pass inside `tools/pr-review` (`harness/runners/`). For small bugfix-shaped PRs, it traces each changed line back to the PR that introduced it. It checks whether that original PR carried a review comment on those lines. If an AI backend judges that the comment would have prevented the bug now being fixed, it posts a comment on the current PR linking to it. The goal is to surface a pattern ("this kind of comment keeps getting ignored") that is otherwise buried in review history no one re-reads.

## 2. Goals

- Detect, for a small bugfix PR, whether any changed line traces back (via `git blame`) to a commit whose originating PR received a review comment on that same line or region.
- Use the AI backend to judge whether addressing that comment back then would have prevented the bug being fixed now. Base the judgment on the current PR's diff (the fix) and the original PR's review comment. Exclude comments that are unrelated, stylistic, or already addressed.
- Post a single comment on the current PR covering every genuine "regret" finding from that run. Link each finding to its original review comment, its PR, and ideally the specific comment or URL. Gate it with a completion marker so a rerun does not repost.
- Gate the whole pass behind a `[regret_review]` block in `.harness.toml`. Mirror the shape of the existing `[vibe_heal]` block: an `enabled` flag plus its own settings, off by default.
- Only run against PRs that look bugfix-shaped. Use a configurable threshold for the maximum number of changed lines (the issue's "≤N lines, configurable"). This keeps the pass cheap and its signal precise: large PRs would blame far more lines than a human could usefully read.

## 3. Non-goals

- Not a general "find stale review comments" auditor. It only looks at comments on lines that a *current, small, bugfix-shaped* PR touches. It never proactively scans old PRs on its own.
- Not responsible for judging whether the *current* PR's fix is itself correct. That is the job of design-review and self-review. `regret-review` only judges the connection between an old comment and the new fix.
- Not extending to non-GitHub-native history. If the introducing commit cannot be mapped to a PR (for example, it was pushed directly to the base branch, or the repo or PR was deleted), the pass skips that line rather than guessing.
- Not building a shared library for git blame, diff, and log parsing. Per dotharness#21's decisions log, that extraction is deferred until a third consumer needs it. So `regret-review` implements its own blame and PR-lookup plumbing directly in this runner, alongside vibe-heal's and code-health's separate copies.
- Not a subprocess or JSON integration with an external tool the way `[vibe_heal]` is. Issue #33 only asks `regret-review` to *mirror that block's config style*. It lives in-process in `harness/runners/`, like `self_review` and `review_requested`, not as a wrapper around a separately installed CLI.

## 4. Glossary

- **Current PR** — the PR under review right now, the one a `regret-review` run is evaluating. Its diff is "the bug now being fixed."
- **Introducing commit** — the commit that `git blame` attributes a changed line's *prior* content to. That is the base-branch version of the line or lines the current PR's diff replaces or removes, not the new lines the fix adds.
- **Introducing PR** — the pull request that merged the introducing commit into the base branch. Resolved via GitHub's "list pull requests associated with a commit" API, not by parsing merge-commit messages.
- **Regret finding** — a case where the introducing PR carried a review comment on the blamed line or region, and the backend judges that addressing it back then would have prevented the bug the current PR fixes.
- **Regret comment** — the comment that `regret-review` posts on the current PR. It links to the original (ignored) review comment and is marked with its own completion marker (mirroring `INLINE_REVIEW_MARKER` and `DESIGN_REVIEW_MARKER` in `harness/runners/common.py`).
- **Bugfix-shaped PR** — a PR whose total changed-line count is at or below a configurable threshold, this pass's proxy for "small, focused fix" per issue #33.

## 5. Key decisions already implied by the brief

- **Blame targets the pre-fix content, not the fix itself.** "For each changed line" in issue #33 must mean the lines the current PR's diff *removes or modifies*. Blaming those against the base branch is what finds the commit and PR that introduced the bug. Blaming the PR's newly *added* lines would only ever point back at the current PR's own commit, which is meaningless.
- **Commit → PR resolution uses GitHub's own API**, not commit-message parsing. `GET /repos/{owner}/{repo}/commits/{sha}/pulls` (exposed via `gh api repos/{owner}/{repo}/commits/{sha}/pulls`) returns the PRs that carried a given commit. This sidesteps unreliable merge-commit-message scraping and works regardless of squash, merge, or rebase strategy. So it is treated as settled, not left open.
- **Original-PR comments are fetched with the existing `fetch_pr_comments` shape** (`harness/runners/common.py`, which shells out to `scripts/pr-comments.py fetch --pr N`). It is confirmed to accept any PR number via `gh pr view N` and is not restricted to the current branch's PR. So no new fetch mechanism is needed. The existing call is pointed at the introducing PR's number instead of the current one.
- **Config shape mirrors `VibehealConfig`** (`harness/config.py`). It is a `RegretReviewConfig` dataclass: `enabled: bool = False` plus its own settings. It parses from a `[regret_review]` `.harness.toml` table the same way `[vibe_heal]` does.
- **Orchestration follows the existing PR-level-pass skeleton** (`run_pr_level_pass` in `harness/runners/common.py`): skip if already posted → build the prompt → invoke the backend → verify the completion marker landed. This is the same skeleton that `self_review.py`'s design and traceability passes already use, not a new orchestration pattern.
- **Hooks into `self_review` and `review_requested`, not `review_prs`.** It is scoped to PRs a human is paying attention to right now: the current user's own open PRs (`self_review`) and PRs where review was explicitly requested from the harness account (`review_requested`). It does not do a full batch sweep of every open PR in the repo. This mirrors where the existing design-review and traceability-review passes already run, in both runners. So `regret_review` joins them as a third pass in the same two places, not a new integration point via `review_prs`.
- **Line matching uses a diff-hunk tolerance window, not exact file and line.** A review comment on the introducing PR counts as a match if its path and line fall within the same contiguous diff hunk (the unified-diff `@@` region) that the introducing commit's change touched, not only on the exact blamed line. This absorbs line drift from force pushes, later commits within the same PR, or minor renumbering. It still requires the same file and the same localized region of change, not a whole-file or whole-PR match.
- **`[regret_review]` config shape**, mirroring `VibehealConfig`'s style. It has `enabled: bool = False`, `max_diff_lines: int = 50`, `authors: str | list[str] = "*"`, and `regret_review_timeout: int = 300`. `max_diff_lines` is the bugfix-shaped threshold, measured the same way `build_file_review_section` counts diff lines: added and removed lines, summed across every changed file in the PR. `authors` mirrors `VibehealConfig.authors`. `regret_review_timeout` is the per-PR wall-clock budget for this pass's git and `gh` calls plus the backend invocation, because blame, commit to PR lookup, and comment fetch can mean several sequential API calls per changed line.
- **A line whose introducing commit or PR cannot be resolved is silently skipped**, not logged as an error or surfaced to the user. This happens when the PR was deleted, the commit was pushed directly to the base branch, or the repo is private or renamed. That is consistent with the fail-open convention every existing `common.py` helper follows for inconclusive lookups, for example `list_open_prs_matching_authors` and `resolve_linked_tickets`.
