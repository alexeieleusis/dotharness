# regret-review — Overview

## 1. Problem statement

When a bug is fixed, the code being corrected was very often reviewed once already —
and sometimes a reviewer flagged the exact issue back then, in a comment that was
never addressed. That "regret" is invisible today: nothing connects a bugfix PR back
to the review comment on the original PR that introduced the bug, so the same kind of
oversight (from the same author, or a different one) tends to recur unremarked.

`regret-review` is a new pass inside `tools/pr-review` (`harness/runners/`) that, for
small bugfix-shaped PRs, traces each changed line back to the PR that introduced it,
checks whether that original PR carried a review comment on those lines, and — if an
AI backend judges that comment would have prevented the bug now being fixed — posts a
comment on the current PR linking back to it. The goal is to surface a pattern
("this kind of comment keeps getting ignored") that's otherwise buried in review
history no one re-reads.

## 2. Goals

- Detect, for a small bugfix PR, whether any changed line traces back (via `git
  blame`) to a commit whose originating PR received a review comment on that same
  line/region.
- Use the AI backend to judge — given the current PR's diff (the fix) and the
  original PR's review comment — whether addressing that comment back then would have
  prevented the bug being fixed now, filtering out comments that are unrelated,
  stylistic, or already addressed.
- Post exactly one comment on the current PR per genuine "regret" finding, linking
  back to the original review comment (its PR, and ideally the specific comment/URL),
  gated by a completion marker so a rerun doesn't repost.
- Gate the whole pass behind a `[regret_review]` block in `.harness.toml`, mirroring
  the existing `[vibe_heal]` block's shape: an `enabled` flag plus its own settings,
  off by default.
- Only run against PRs that look bugfix-shaped: a configurable max-changed-lines
  threshold (the issue's "≤N lines, configurable"), keeping the pass cheap and its
  signal precise (large PRs would blame far more lines than a human could usefully
  read regret findings about).

## 3. Non-goals

- Not a general "find stale review comments" auditor — it only looks at comments on
  lines that a *current, small, bugfix-shaped* PR is touching. It never proactively
  scans old PRs on its own.
- Not responsible for judging whether the *current* PR's fix is itself correct — that
  is design-review's/self-review's job. `regret-review` only judges the connection
  between an old comment and the new fix.
- Not extending to non-GitHub-native history: if the introducing commit can't be
  mapped to a PR (e.g. pushed directly to the base branch, or the repo/PR was deleted),
  the pass skips that line rather than guessing.
- Not building a shared git-blame/diff/log-parsing library. Per dotharness#21's
  decisions log, that extraction is deferred until a third consumer actually needs it
  — `regret-review` implements its own blame/PR-lookup plumbing directly in this
  runner, alongside vibe-heal's and code-health's separate copies.
- Not a subprocess/JSON integration with an external tool the way `[vibe_heal]` is.
  Issue #33 only asks `regret-review` to *mirror that block's config style* — it lives
  in-process in `harness/runners/`, like `self_review`/`review_requested`, not as a
  wrapper around a separately-installed CLI.

## 4. Glossary

- **Current PR** — the PR under review right now, the one a `regret-review` run is
  evaluating; its diff is "the bug now being fixed."
- **Introducing commit** — the commit `git blame` attributes a changed line's *prior*
  content to (i.e., blaming the base-branch version of the line(s) the current PR's
  diff replaces or removes, not the new lines the fix adds).
- **Introducing PR** — the pull request that merged the introducing commit into the
  base branch. Resolved via GitHub's "list pull requests associated with a commit"
  API, not by parsing merge-commit messages.
- **Regret finding** — a case where the introducing PR carried a review comment on the
  blamed line/region, and the backend judges that addressing it back then would have
  prevented the bug the current PR fixes.
- **Regret comment** — the comment `regret-review` posts on the current PR, linking
  back to the original (ignored) review comment, marked with its own completion marker
  (mirroring `INLINE_REVIEW_MARKER` / `DESIGN_REVIEW_MARKER` in `harness/runners/common.py`).
- **Bugfix-shaped PR** — a PR whose total changed-line count is at or below a
  configurable threshold, this pass's proxy for "small, focused fix" per issue #33.

## 5. Key decisions already implied by the brief

- **Blame targets the pre-fix content, not the fix itself.** "For each changed line"
  in issue #33 must mean the lines the current PR's diff *removes/modifies* — blaming
  those against the base branch is what finds the commit (and PR) that introduced the
  bug. Blaming the PR's newly *added* lines would only ever point back at the current
  PR's own commit, which is meaningless.
- **Commit → PR resolution uses GitHub's own API**, not commit-message parsing:
  `GET /repos/{owner}/{repo}/commits/{sha}/pulls` (exposed via `gh api
  repos/{owner}/{repo}/commits/{sha}/pulls`) returns the PR(s) that carried a given
  commit. This sidesteps unreliable merge-commit-message scraping and works
  regardless of squash/merge/rebase strategy, so it is treated as settled rather than
  left open.
- **Original-PR comments are fetched with the existing `fetch_pr_comments` shape**
  (`harness/runners/common.py`, itself shelling out to `scripts/pr-comments.py fetch
  --pr N`) — confirmed to accept an arbitrary PR number via `gh pr view N`, not
  restricted to the current branch's PR, so no new fetch mechanism is needed, only a
  call pointed at the introducing PR's number instead of the current one.
- **Config shape mirrors `VibehealConfig`** (`harness/config.py`): a `RegretReviewConfig`
  dataclass, `enabled: bool = False` plus its own settings, parsed from a
  `[regret_review]` `.harness.toml` table the same way `[vibe_heal]` is — see
  `OPEN_QUESTIONS.md` for exactly which settings.
- **Orchestration follows the existing PR-level-pass skeleton**
  (`run_pr_level_pass` in `harness/runners/common.py`): skip-if-already-posted → build
  prompt → invoke the backend → verify the completion marker landed — the same
  skeleton `self_review.py`'s design/traceability passes already use, rather than a
  new orchestration pattern.
- **Hooks into `self_review` and `review_requested`, not `review_prs`.** Scoped to
  PRs a human is actually paying attention to right now — the current user's own open
  PRs (`self_review`) and PRs where review was explicitly requested from the harness
  account (`review_requested`) — rather than a full batch sweep of every open PR in
  the repo. This mirrors where the existing design-review/traceability-review passes
  already run (both runners), so `regret_review` joins them as a third pass in the
  same two places rather than opening a new integration point via `review_prs`.
- **Line matching uses a diff-hunk tolerance window, not exact file+line.** A review
  comment on the introducing PR counts as a match if its (path, line) falls within the
  same contiguous diff hunk (unified-diff `@@` region) that the introducing commit's
  change touched — not only on the exact blamed line. This absorbs line drift from
  force-pushes, later commits within the same PR, or minor renumbering, while still
  requiring the same file and the same localized region of change (not a whole-file or
  whole-PR match).
- **`[regret_review]` config shape**, mirroring `VibehealConfig`'s style:
  `enabled: bool = False`, `max_diff_lines: int = 50` (the bugfix-shaped threshold,
  measured the same way `build_file_review_section` already counts diff lines: added +
  removed lines, summed across every changed file in the PR), `authors: str | list[str]
  = "*"` (mirrors `VibehealConfig.authors`), and `regret_review_timeout: int = 300`
  (per-PR wall-clock budget for this pass's git/`gh` calls plus backend invocation,
  since blame + commit→PR lookup + comment fetch can mean several sequential API calls
  per changed line).
- **A line whose introducing commit/PR can't be resolved (deleted PR, direct push to
  the base branch, private/renamed repo) is silently skipped**, not logged as an error
  or surfaced to the user — consistent with the fail-open convention every existing
  `common.py` helper already follows for inconclusive lookups (e.g.
  `list_open_prs_matching_authors`, `resolve_linked_tickets`).
