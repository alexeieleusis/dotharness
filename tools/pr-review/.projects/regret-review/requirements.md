# Requirements: "Regret Review" — Surfacing Ignored Past Review Comments

> Drafted per `~/.harness/knowledge/spec-prism-flow/requirements-doc-drafting-prompt.md`,
> from [dotharness#33](https://github.com/alexeieleusis/dotharness/issues/33) (part of
> [dotharness#21](https://github.com/alexeieleusis/dotharness/issues/21)). This is the
> single source of truth a later, smaller work order (a spec-prism-flow phase file) will
> quote from — not the work order itself. Every ambiguity the issue left open was raised
> in `00-overview.md` / the original `OPEN_QUESTIONS.md` and has since been resolved —
> resolutions are folded into §7/§8 below and marked with pointers so nothing silently
> disappears (§11).

## 1. Purpose / origin

Issue #33 is a one-line brief. For a small, bugfix-shaped PR: blame each changed line
back to the commit that introduced it, find that commit's PR, and check whether that PR
carried a review comment on those lines. If an AI backend judges that the comment
would have prevented the bug now being fixed, post a comment on the current PR linking
back to it. This requirement is not derived from a reference implementation. It generalizes an
observation (review comments get raised and then silently ignored) into a mechanical
check.

It is the third PR-level pass added to `tools/pr-review`'s existing `self-review` /
`review-requested` runners, after the design-review pass
(`design-review-requirements.md`, dotharness#3) and the requirement-traceability pass
(`requirement-traceability-requirements.md`, dotharness#4). Both of those documents, and
the code they produced, are this document's primary reference implementation: the same
`run_pr_level_pass` orchestration skeleton, the same per-pass completion-marker
convention, the same `self_review.py`/`review_requested.py` wiring shape. This document
reuses that shape rather than inventing a new one.

Per dotharness#21's decisions log, this pass is explicitly **not** a subprocess/JSON
integration with a separate tool the way `[vibe_heal]` is. Issue #33 only asks it to
*mirror that block's config style* (an `enabled`-gated `.harness.toml` table). The
pass itself lives in-process in `harness/runners/`, alongside `self_review.py` and
`review_requested.py`. Dotharness#21 also flags git-blame/diff/log-parsing plumbing as a
second candidate for future shared-library extraction (alongside vibe-heal's `git/` and
code-health's churn loader) — **deferred until a third consumer needs it, not built
here.**

## 2. Problem statement

Today, `self-review` and `review-requested` run three passes per PR. The first is
per-file correctness review, which also produces a PR-level summary
(`review-file.md` and `review-summary.md` together). The second is design review
(`review-design.md`). The third is requirement-traceability review
(`review-traceability.md`). None of them look backward in time. A reviewer's comment on the original PR that
introduced a bug — "this edge case isn't handled," "this will break if X" — is either
addressed then, or it silently disappears into review history nobody re-reads. When the
predicted bug eventually surfaces and gets fixed in a later, unrelated PR, there is no
mechanism connecting the fix back to the comment that would have prevented it. The
person who ignored (or missed) that comment gets no signal that this specific class of
oversight is recurring. A different author fixing the same code has no way to discover
the earlier warning at all.

This is a distinct failure mode from what the other three passes already catch.
Correctness review only looks at the *current* diff in isolation. Design review and
traceability review only look at the current PR's relationship to its own linked
ticket. None of them have any notion of "this file's history already contains a
relevant, unaddressed comment."

## 3. Goals

- **G1.** For every small, bugfix-shaped PR run through `self-review` or
  `review-requested` (§7.3, §7.4), for every changed line in that PR's diff, identify
  the commit (and, transitively, the PR) that most recently touched that line's
  *prior* content before this PR's fix (§7.1).
- **G2.** For each such introducing PR, fetch its review comments and identify any
  whose (path, line) falls within the same diff hunk as the blamed change (§7.2,
  resolved tolerance model — not exact-line-only).
- **G3.** For each candidate comment found by G2, use the AI backend to judge whether
  addressing it, at the time it was made, would have prevented the bug the current PR
  is now fixing. Only a positive judgment produces a finding (§7.2).
- **G4.** Post at most one PR-level comment on the current PR per run, listing every
  confirmed regret finding and linking back to each original comment (its PR number and,
  where resolvable, its direct GitHub URL), gated by its own completion marker so a
  rerun never reposts (§7.6).
- **G5.** The pass runs unconditionally as part of `self-review` and
  `review-requested` whenever `[regret_review].enabled = true` in `.harness.toml` — the
  first PR-level pass in this codebase to be independently toggleable, unlike design
  review and traceability review, which always run (§7.5, §9).
- **G6.** The pass never runs on a PR whose total changed-line count exceeds
  `[regret_review].max_diff_lines` (default 50) — the issue's "≤N lines, configurable"
  requirement (§7.1, §7.5).
- **G7.** The pass's own git-blame/PR-lookup/comment-matching plumbing lives entirely
  inside this pass's own module — no shared library is extracted for it in this effort
  (§4, dotharness#21).

## 4. Non-goals

- **Not wiring into `review-prs`.** Same rationale as design review's non-goal
  (`design-review-requirements.md` §4): `review-prs` uses a structurally different
  pipeline (the `[vibe_heal]` integration), with no existing per-file/summary/design/
  traceability pass to sit alongside. Confirmed for this effort, not merely inherited by
  default (resolved via the project's own open-questions checkpoint, §11.1).
- **Not judging the correctness of the current PR's fix.** That is correctness
  review's (`review-file.md`) job. `regret-review` only judges the *connection* between
  an old comment and the new fix — never whether the fix itself is adequate.
- **Not a general "stale review comment" auditor.** It never proactively scans old PRs
  on its own initiative. It only looks at comments on lines a *current, small,
  bugfix-shaped* PR happens to be touching, right now.
- **Not building a shared git-blame/diff/log-parsing library.** Per dotharness#21's
  decisions log, this pass implements its own blame/commit→PR/comment-matching
  plumbing directly, the same way vibe-heal's `git/` and code-health's churn loader do
  independently today. Extraction is explicitly deferred until a third real consumer
  needs it.
- **Not extending to non-GitHub-native history.** If the introducing commit can't be
  resolved to any PR (direct push to the base branch, deleted PR, private/renamed
  repo), that line is silently skipped — never logged as an error, never surfaced to a
  human (§7.1, resolved).
- **Not adding a new git-blame-based UI, dashboard, or report** beyond the single PR
  comment G4 describes. No standalone CLI command, no aggregate "regret rate" metric.

## 5. Glossary

- **Current PR** — the PR under review right now, the one a `regret-review` run is
  evaluating. Its diff is "the bug now being fixed."
- **Introducing commit** — the commit `git blame` attributes a changed line's *prior*
  content to: blame the base-branch version of the line(s) the current PR's diff
  removes or modifies, never the new lines the fix adds (blaming the fix's own added
  lines would only ever point back at the current PR's own commit).
- **Introducing PR** — the pull request GitHub's API reports as containing the
  introducing commit, via `GET /repos/{owner}/{repo}/commits/{sha}/pulls` (exposed via
  `gh api repos/{owner}/{repo}/commits/{sha}/pulls`). Chosen over commit-message
  parsing because it works regardless of merge/squash/rebase strategy.
- **Diff hunk** — a contiguous, unified-diff `@@ -a,b +c,d @@`-delimited region of
  change. Used here as the tolerance boundary for matching a review comment's (path,
  line) against a blamed change's location (§7.2) — not the same as "the whole file."
- **Regret finding** — a case where the introducing PR carried a review comment
  matching within the same diff hunk as the introducing change, and the backend judges
  that addressing it back then would have prevented the bug the current PR fixes.
- **Regret comment** — the PR-level comment `regret-review` posts on the current PR,
  carrying every confirmed regret finding for that run, marked with its own completion
  marker (mirroring `INLINE_REVIEW_MARKER` / `DESIGN_REVIEW_MARKER` /
  `TRACEABILITY_REVIEW_MARKER` in `harness/runners/common.py:23-26`).
- **Bugfix-shaped PR** — a PR whose total changed-line count (added + removed, summed
  across every changed file, the same measure `build_file_review_section`
  (`harness/runners/common.py:1101-1133`) already uses per-file) is at or below
  `[regret_review].max_diff_lines`.
- **PR-level pass** — as defined in `design-review-requirements.md` §5: one backend
  invocation per PR, given the whole PR's diff/file set as context, as opposed to a
  per-file pass.

## 6. Feature/component breakdown

| Component | Question it answers | Input | Output |
|---|---|---|---|
| Blame/introducing-commit resolver (§7.1) | Which commit(s) last touched the lines this PR's fix changes? | Current PR's diff (per file, per hunk) | List of `(file, line_range, introducing_sha)` |
| Commit→PR resolver (§7.1) | Which PR merged that introducing commit? | `introducing_sha` | Introducing PR number (or none) |
| Comment fetch + hunk-match (§7.2) | Did the introducing PR carry a review comment on that region? | Introducing PR number, blamed line range | Candidate `(comment, blamed_change)` pairs |
| Backend judgment (§7.2) | Would addressing that comment have prevented this bug? | Candidate pair + current PR's fix diff | Confirmed regret finding, or discarded |
| Comment poster (§7.6) | Post the findings, once, idempotently | Confirmed findings for this run | One PR-level comment on the current PR |
| Config (§7.5) | Is this pass on, and for whom/how big a PR? | `.harness.toml` `[regret_review]` | `RegretReviewConfig` |
| Runner wiring (§7.3, §7.4) | When does this pass run? | `self_review.py` / `review_requested.py` per-PR loop | Pass invoked or skipped |

## 7. Detailed functional requirements

### 7.1 Blame → introducing commit → introducing PR

For the current PR's diff against its base branch (the same `origin/{base}...HEAD`
comparison `get_changed_files`/`get_file_diff` already use,
`harness/runners/common.py:1066-1098`):

1. **Bugfix-shaped gate, checked first.** Sum added + removed lines across every
   changed file (same counting rule as `build_file_review_section`'s diff-line count,
   `harness/runners/common.py:1117-1119`). If the total exceeds
   `config.regret_review.max_diff_lines`, skip this pass entirely for this PR — no
   blame, no backend call, no comment. This is a cheap, early exit.
2. **Per file, per removed/modified line range:** run `git blame` against the
   pre-fix state — i.e. blame `origin/{base_branch}` (or the PR's merge-base with it)
   for the file, restricted to the line ranges the current PR's diff hunks mark as
   removed or changed (`git blame -L <start>,<end> <base_ref> -- <path>`, once per
   contiguous removed/changed range per file). Newly *added* lines with no prior
   content are not blamed — there is nothing to trace back.
3. **Deduplicate** by introducing commit SHA within a single run: a range of lines that
   all blame to the same commit is treated as one candidate rather than re-processing
   the same commit repeatedly (this repo's file may plausibly have many small hunks
   blaming to the same earlier commit).
4. **Resolve each unique introducing SHA to its PR(s)** via `gh api
   repos/{owner}/{repo}/commits/{sha}/pulls`. If this returns zero PRs (direct push,
   deleted PR, or the API call itself fails/rate-limits), **skip that commit's lines
   silently** — no error logged above debug level, no partial finding, consistent with
   the fail-open convention every existing helper in `harness/runners/common.py`
   already follows for inconclusive lookups (e.g. `list_open_prs_matching_authors`,
   `resolve_linked_tickets`). If it returns more than one PR (e.g. cherry-picked
   across branches), evaluate every returned PR independently — a comment on any one of
   them is a valid candidate.

### 7.2 Comment matching and backend judgment

1. **Fetch the introducing PR's comments** using the existing `fetch_pr_comments`
   shape (`harness/runners/common.py:1223-1267`, itself shelling out to
   `scripts/pr-comments.py fetch --pr N`) — confirmed to accept an arbitrary PR number
   via `gh pr view N`, not restricted to the current branch's PR (verified directly
   against `scripts/pr-comments.py`'s `cmd_fetch`), so this requires no new fetch
   mechanism, only pointing the existing call at the introducing PR's number instead
   of the current one. Only `inline`/`review`-typed comments carry a `path`/`line`
   (`common.py`'s `fetch_pr_comments` docstring). `issue`-typed comments have neither
   and are never candidates for a location match.
2. **Hunk-tolerance match, not exact-line match (resolved, §11.2).** A fetched comment
   is a candidate if its `path` matches the blamed file and its `line` falls within
   the same contiguous diff hunk (in the introducing PR's own diff, at the commit the
   comment was posted against) that the introducing commit's change occupied — not
   only the exact blamed line. This absorbs drift from force-pushes or later commits
   within that same PR while still requiring the same localized region of change, not
   a whole-file match. (Computing "the same diff hunk" means diffing the introducing
   commit against its own parent, restricted to the blamed file, and taking that
   hunk's `@@` line range as the match window — not the current PR's hunks, which are
   a different diff entirely.)
3. **One backend invocation per current-PR run, not per candidate.** Mirroring
   `_run_design_review`/`_run_traceability_review`'s one-call-per-PR shape
   (`harness/runners/self_review.py:221-259`), every candidate `(comment,
   blamed_change)` pair found across every file in the current PR is batched into a
   single prompt. The prompt gives the backend: the current PR's fix diff (via
   `build_file_review_section`, `harness/runners/common.py:1101-1133`, same shape
   every other pass already uses) and, per candidate, the original comment's body,
   author, and the diff of the change it was left on. The backend returns which
   candidates (if any) it judges as genuine regrets — this pass does not attempt to
   parse free-form prose back out of a plain-text response. The prompt must ask for a
   structured (e.g. one-candidate-per-line) answer the poster (§7.6) can parse
   deterministically before treating anything as confirmed.
4. **No candidates found (either no blame hits, or blame hits but no PR/comment
   match).** The pass performs no backend invocation and posts nothing — mirroring
   traceability review's "no linked ticket" short-circuit
   (`harness/runners/common.py:723-750`) in spirit (skip the backend call when there is
   structurally nothing to judge), though regret-review's silent-skip is terminal only
   for *this run*, not permanently marked done the way "no linked ticket" is (a later
   PR update could touch different lines with different blame history).

### 7.3 Wiring into `self-review`

Follows `_run_design_review`'s exact shape (`harness/runners/self_review.py:221-259`):

- A new `_run_regret_review` function, invoked from `_run_locked`'s per-PR loop
  (`harness/runners/self_review.py:508-519`) alongside the existing `run_design`/
  `run_traceability` calls, gated additionally on
  `config.regret_review.enabled` (§7.5) — when `false`, this pass is never invoked at
  all, not merely skipped per-PR (contrast with design/traceability review, which have
  no such config gate at all, §9).
- Own persisted state: `regret_reviewed_prs`, mirroring `design_reviewed_prs`'
  read/write/prune shape (`harness/state.py`'s `prune_self_review_state`/
  `add_design_reviewed_pr` equivalents) — tracked independently so this pass's retry
  never forces, or is forced by, the per-file review's or design review's retry
  behavior, per the same rationale `_run_design_review`'s docstring gives.
- Orchestration itself (noop-if-done → build prompt → run backend → re-verify marker)
  reuses `common.run_pr_level_pass` (`harness/runners/common.py:752-816`) unchanged —
  no new orchestration skeleton.
- Own instructions file, `review-regret.md`, read from the same
  `knowledge_dir / "pr-review"` directory as the other three
  (`harness/runners/self_review.py:498-502`), authored alongside this effort (not
  scaffoated automatically — the instructions file's prose content is this project's
  own deliverable, following `review-design.md`'s existing structure/audience as a
  model).

### 7.4 Wiring into `review-requested`

Follows `_run_design_review`'s exact shape in this runner
(`harness/runners/review_requested.py:320-388`):

- A new `_run_regret_review` function alongside the file/summary/design/traceability
  calls in this runner's per-PR loop (`harness/runners/review_requested.py:205-221`),
  gated the same way as §7.3 on `config.regret_review.enabled`.
- **No persisted state**, exactly like design/traceability review in this runner
  (`review_requested.py:332-335`): `has_comment_fn` (the regret-comment marker check)
  is this pass's only "already done" signal here, since `review_requested` has no
  state file at all.
- Same `review-regret.md` instructions file, same `run_pr_level_pass` reuse.

### 7.5 `.harness.toml` config schema

A new `RegretReviewConfig` dataclass in `harness/config.py`, added to `HarnessConfig`
the same way `VibehealConfig`/`FocusedReviewConfig`/`AddressCommentsConfig` already are
(`harness/config.py:36-58, 80-86`), parsed from a `[regret_review]` table
(`harness/config.py:100-198`'s `load_config` pattern):

```python
@dataclass
class RegretReviewConfig:
    enabled: bool = False
    max_diff_lines: int = 50
    authors: str | list[str] = "*"
    regret_review_timeout: int = 300
```

- `enabled` — off by default, per G5/§9. This is the only toggleable pass in this codebase.
- `max_diff_lines` — the bugfix-shaped threshold (§7.1 step 1). Default 50, chosen as a
  round, conservative starting point for "small fix" pending real usage data (no
  existing corpus to derive this empirically from, unlike `docs/requirements.md`'s
  sizing bands elsewhere in this monorepo — flagged here rather than invented as if
  measured).
- `authors` — mirrors `VibehealConfig.authors` (`harness/config.py:37-46`): `"*"` (all
  authors) or an explicit allowlist of GitHub usernames, applied to the **current**
  PR's author (the introducing PR's author is never filtered — the point is tracing
  *whoever's* old comment was ignored).
- `regret_review_timeout` — a per-PR wall-clock budget covering this pass's own
  git/`gh` calls (blame, commit→PR lookups, comment fetches) plus the single backend
  invocation, separate from `harness.backend_timeout_seconds` since this pass can issue
  several sequential `gh api` calls per PR before ever reaching the backend.

`docs/configuration.md`'s schema tables need a new `[regret_review]` section, mirroring
the existing `[vibe_heal]`/`[focused_review]` entries there, once this shape is
implemented.

### 7.6 Regret comment format, marker, and idempotency

- **New marker constant** in `harness/runners/common.py`, alongside
  `INLINE_REVIEW_MARKER`/`DESIGN_REVIEW_MARKER`/`TRACEABILITY_REVIEW_MARKER`
  (`common.py:23-26`): e.g. `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"`.
- **Idempotency and flagged-locations helpers** reuse the existing generic
  `has_pr_level_pass_comment`/`check_pr_level_pass_comment_status`
  (`common.py:368-387`) and `get_pr_level_flagged_locations` (`common.py:426-451`) —
  already parameterized by `marker`, so no new per-pass duplicate of those helpers is
  needed, only thin `has_regret_review_comment`/`check_regret_review_comment_status`
  wrappers mirroring `has_design_review_comment`/`check_design_review_comment_status`
  (`common.py:394-404`).
- **Comment body**: one PR-level comment (never inline — a regret finding's "location"
  is in the *introducing* PR, not the current one, so an inline comment on the current
  PR's diff would misleadingly anchor it to the wrong place), listing every confirmed
  regret finding for this run: which file/region, the introducing PR number and a link
  to the original comment (constructed from the PR number + comment id the same way
  `fetch_pr_comments`'s cached shape already carries — `scripts/pr-comments.py`'s
  fetched comment dicts include enough to build a direct GitHub URL), and a short
  quote of the original comment body. Ends with `REGRET_REVIEW_MARKER`.
- **A rerun that finds new regret candidates on an already-regret-commented PR**
  behaves like every other PR-level pass here: `is_done`/`has_comment_fn` gate whether
  the backend runs at all, so once a regret comment has been posted for this PR, this
  pass does not re-run for it in this runner cycle. (No "append new findings to an
  existing comment" mechanism is in scope — see §9.)

## 8. Non-functional requirements

- **Cost bound follows directly from G6.** The `max_diff_lines` gate (default 50) means
  this pass's per-PR cost (a bounded number of `git blame` calls, one `gh api commits/
  {sha}/pulls` call per unique introducing commit, one `pr-comments.py fetch` per unique
  introducing PR, and at most one backend invocation) never scales with a large PR —
  it either exits at step 1 of §7.1 or operates on a small, bounded line count.
  No empirical corpus exists yet to derive a tighter number than the round default
  above. This is flagged, not invented as if measured (§7.5).
- **Its own bounded per-PR timeout**, `regret_review_timeout` (§7.5, default 300s),
  covering every git/`gh` call this pass makes plus its single backend invocation —
  separate from `harness.backend_timeout_seconds`, which only bounds the backend call
  itself.
- **Rate-limit awareness.** `gh api commits/{sha}/pulls` and the comment fetch are both
  authenticated GitHub API calls subject to the same rate limits every other `gh`-based
  helper in `harness/runners/common.py` already operates under (`TIMEOUT_GH = 30`,
  `common.py:16`). No new rate-limit-specific handling is introduced beyond the
  existing fail-open pattern (§7.1 step 4) — a rate-limited lookup is treated the same
  as "no PR found," not retried within the same run.
- **No new external dependency.** Every mechanism here (`git blame`, `gh api`, the
  existing `pr-comments.py` script, the existing `Backend` abstraction) is already
  present in this codebase. This pass adds no new subprocess tool, package, or service.

## 9. Out-of-scope / explicit exclusions

- **Not toggle-free like design/traceability review.** Those two passes run
  unconditionally. This one is explicitly config-gated (G5) because, unlike them, it
  has real per-run cost variance (blame + multiple `gh api` calls) that a repo owner
  may reasonably want off by default — this is a deliberate asymmetry with the
  existing two passes, not an oversight.
- **Not appending to, or editing, a previously-posted regret comment** if a later run
  on the same PR finds additional candidates (§7.6). A rerun after the marker is
  already posted is a full noop for this pass, exactly like design/traceability review
  today.
- **Not attempting fuzzy content-based line matching** (e.g. matching moved/renamed
  code by diffing content rather than following git's own rename detection). Only
  `git blame`'s own commit attribution and the introducing PR's own diff hunks are
  used. If `git blame` itself loses the line across a rename it doesn't follow, that
  line is simply not traced — no additional heuristic is layered on top in this
  effort.
- **Not building the requirement-traceability pass's early-comment-window pattern**
  (`TRACEABILITY_COMMENT_WINDOW_SECONDS`, `common.py:35-37`) into this pass — that
  window is specific to catching clarifying comments posted right after a PR opens,
  which has no analog here (this pass looks at all of an old PR's review comments,
  not a time-boxed window of its issue comments).

## 10. System/tool shape

- **Location:** a new module, `harness/runners/regret_review.py`, alongside
  `self_review.py`/`review_requested.py`/`review_prs.py` in
  `tools/pr-review/harness/runners/`. Shared plumbing (blame, commit→PR resolution,
  hunk matching) lives as private helpers in this same module per §4/G7 — not
  extracted into `harness/runners/common.py` or a new top-level package, since it is
  used from exactly this one module (called from both `self_review.py` and
  `review_requested.py`, but the *implementation* of the blame/lookup logic itself has
  one owner).
- **Config:** `RegretReviewConfig` in `harness/config.py` (§7.5), parsed as part of the
  existing `.harness.toml` `load_config` (`harness/config.py:100-198`).
- **Instructions file:** `review-regret.md`, added to the same knowledge directory the
  other three prompt templates already live in (read via `config.harness.knowledge_dir
  / "pr-review"`, e.g. `~/.harness/knowledge/pr-review/review-regret.md`), following
  `review-design.md`'s structure/audience as a model (§7.3).
- **New marker:** `REGRET_REVIEW_MARKER` in `harness/runners/common.py` (§7.6).
- **Docs:** `docs/configuration.md` gets a new `[regret_review]` schema section (§7.5).
  This document itself (`regret-review-requirements.md`, or wherever this project's
  `.projects/regret-review/requirements.md` is ultimately checked in) becomes the
  sibling of `design-review-requirements.md` /
  `requirement-traceability-requirements.md` at the repo root, once approved.
- **Tests:** unit coverage for the blame/hunk-matching logic (fixture-based, not
  hitting real `gh`/`git blame` against GitHub) mirroring the existing test structure
  for `self_review.py`/`review_requested.py`/`common.py` in `tools/pr-review/tests/`.

## 11. Decisions log (resolved via this project's own open-questions checkpoint)

1. ~~**Which runner command(s) does this pass hook into?**~~ **Resolved:** both
   `self-review` and `review-requested` (§7.3, §7.4) — not `review-prs` (§4). Scoped to
   PRs a human is actually paying attention to right now, matching where design review
   and traceability review already run, rather than opening a new integration point.
2. ~~**Line-range matching strictness under drift** (force-pushes, renames, later
   commits within the same introducing PR)~~. **Resolved:** same-diff-hunk tolerance,
   not exact file+line (§7.2 step 2, glossary "Diff hunk"). Chosen over exact-line
   matching to absorb realistic drift while still requiring the same localized region
   of change, not a whole-file match.
3. ~~**`[regret_review]` config shape beyond `enabled`.**~~ **Resolved:**
   `max_diff_lines` (default 50), `authors` (mirrors `VibehealConfig.authors`), and
   `regret_review_timeout` (default 300s) — see §7.5 for the full dataclass and
   rationale for each field's default.
4. ~~**Behavior when the introducing commit/PR can't be resolved** (deleted PR,
   direct push, private/renamed repo).~~ **Resolved:** silently skip that commit's
   lines, no error surfaced — consistent with every existing fail-open helper in
   `harness/runners/common.py` (§7.1 step 4, §4).

No further open decisions remain from the brief. Any question raised for the first time
during `decompose`/`draft-phases` (e.g. exact hunk-matching algorithm implementation
detail, `review-regret.md`'s precise prose) should be logged fresh at that stage rather
than assumed to be covered here.

## 12. Next step

Once this document is reviewed/approved: `spec-prism-flow plan decompose` against this
`requirements.md`, splitting §7's six functional areas (7.1–7.6) into a dependency-
ordered decomposition tree, followed by `plan draft-phases` to produce one phase file
per leaf (config/dataclass, blame+commit→PR resolver, comment-fetch+hunk-match,
backend-judgment prompt + `review-regret.md` authoring, `self-review` wiring,
`review-requested` wiring, marker/idempotency helpers, `docs/configuration.md` update)
and `plan review` as the exit gate before `build run` starts opening PRs.
