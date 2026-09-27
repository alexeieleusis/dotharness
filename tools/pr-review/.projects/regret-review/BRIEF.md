# regret-review

Source: [alexeieleusis/dotharness#33](https://github.com/alexeieleusis/dotharness/issues/33)
(labels: enhancement)

## Issue title

pr-review: "regret review" runner — surface ignored past review comments on small
bugfix PRs

## Issue body (verbatim)

For small PRs (≤N lines, configurable — a bugfix-shaped change): for each changed
line, `git blame` to the introducing commit, find the PR that introduced it, look for
review comments on those lines in that original PR, then use the AI backend to judge
whether any of those comments — if addressed — would have prevented the bug now being
fixed. If so, post a comment on the current PR linking back to the original ignored
comment.

Lives in `tools/pr-review` (`harness/runners/`), config-gated in `.harness.toml` like
the existing `[vibe_heal]` block.

Part of #21.

## Parent issue context (#21 — architecture direction, verbatim excerpt)

> **Integrate via config + subprocess + JSON, not shared code** — following the
> pattern `pr-review` already uses for `vibe-heal` (the `[vibe_heal]` block in
> `.harness.toml`, which points at vibe-heal's venv and shells out to it).

This applies to `code-health`/`vibe-heal`, which are *separate* tools/repos integrated
by shelling out. `regret-review` is different: issue #33 says it **lives inside**
`tools/pr-review` itself (`harness/runners/`), only *mirroring* `[vibe_heal]`'s
config-block *style* (a `[regret_review]` table in `.harness.toml` with an `enabled`
flag and its own settings) — not a subprocess/JSON integration with an external tool.

> **Two opportunistic shared-library extractions** [...] Git blame/diff/log-parsing
> plumbing is a second, smaller case (vibe-heal's `git/`, code-health's churn loader,
> and the regret-review runner below all need it). **Worth extracting once a third
> consumer actually needs it, not before.**

So: implement git-blame/PR-lookup plumbing directly in this runner, no premature shared
library.

## Existing code to build on (this repo, `tools/pr-review/`)

- `harness/config.py` — `HarnessConfig` and its per-feature dataclasses
  (`VibehealConfig`, `FocusedReviewConfig`, `AddressCommentsConfig`), each with an
  `enabled: bool = False` field and its own settings, parsed from a `.harness.toml`
  table of the same name. A `[regret_review]` block/dataclass should follow this same
  shape.
- `harness/backend.py` — `Backend`, the AI-backend abstraction (`opencode`/`claude`)
  every runner already invokes via `backend.run(prompt, cwd=..., context=...)`.
- `harness/runners/common.py` — shared plumbing every runner already uses:
  `run_cmd` (subprocess wrapper with timeout/SIGTERM-then-SIGKILL), `get_changed_files`
  / `get_file_diff` (git diff against a base branch), `fetch_pr_comments` (shells out to
  `scripts/pr-comments.py fetch --pr N`, returns cached `inline`/`review`/`issue`
  comments for a PR — this is the fetch shape to reuse, pointed at the *original* PR
  instead of the current one), `run_pr_level_pass` (shared orchestration skeleton:
  skip-if-done → build prompt → run backend → verify the completion marker landed —
  same skeleton every existing PR-level pass below already uses),
  `is_pr_open`/`pr_from_url`/`list_open_prs_matching_authors` (gh CLI wrappers),
  `build_file_review_section`/`_build_pr_metadata_trailer` (prompt-building helpers).
- `harness/runners/self_review.py` and `harness/runners/review_requested.py` — the two
  existing PR-level passes (design review, requirement-traceability review) with the
  closest shape to what regret-review needs: diff the PR, build a prompt from
  file/context sections, invoke the backend, post an inline or issue comment, gate on a
  completion marker so a rerun doesn't re-post. `regret-review` should follow this same
  pattern, with its own marker constant (see `INLINE_REVIEW_MARKER` and friends in
  `common.py`) and its own instructions file (see `design-review-requirements.md` /
  `requirement-traceability-requirements.md` at the repo root for the existing
  spec-per-pass convention).
- `harness/runners/review_prs.py` — the existing `[vibe_heal]`-gated runner; useful as
  a second reference for how a config-gated, `enabled`-flag-checked feature is wired
  into a runner and into `harness/cli.py`'s command dispatch.
- `docs/configuration.md` — the `.harness.toml` schema doc; a `[regret_review]` section
  needs to be added here (mirroring the existing `[vibe_heal]`/`[focused_review]`
  section entries) once the config shape is settled.
- `scripts/pr-comments.py` — the script `fetch_pr_comments` shells out to; check
  whether it can fetch an *arbitrary* PR's comments (not just the "current" one being
  reviewed) as-is, since regret-review needs the *original introducing* PR's comments,
  not the PR currently under review.

## Open design questions for the `plan` stage to resolve

- How does "find the PR that introduced it" work concretely, given only an introducing
  commit SHA? (`gh pr list --search <sha>`, `gh api search/issues?q=<sha>`, or walking
  merge commits — needs a reliable, rate-limit-friendly mechanism.)
- What counts as "review comments on those lines in that original PR" when the file has
  since been renamed/moved, or the introducing PR squash-merged (losing per-commit line
  mapping)?
- Where does the "≤N lines" bugfix-shaped-change threshold live in config, and how is
  it measured (changed lines across the whole PR vs. per file)?
- What does the new `[regret_review]` `.harness.toml` block need beyond `enabled` (e.g.
  a line-count threshold, which runner command(s) it hooks into — `review-prs`,
  `self-review`, both)?
- Comment format/marker for the "regret" comment this runner posts, and its
  idempotency check (mirroring `INLINE_REVIEW_MARKER` and `has_inline_review_comments`
  style helpers in `common.py`).
