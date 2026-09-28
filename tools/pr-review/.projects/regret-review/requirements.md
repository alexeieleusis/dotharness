# Requirements: "Regret Review" — Surfacing Ignored Past Review Comments

> This document was drafted per `~/.harness/knowledge/spec-prism-flow/requirements-doc-drafting-prompt.md`,
> from [dotharness#33](https://github.com/alexeieleusis/dotharness/issues/33) (part of
> [dotharness#21](https://github.com/alexeieleusis/dotharness/issues/21)).
> It is the single source of truth that a later, smaller work order (a spec-prism-flow
> phase file) will quote from. It is not the work order itself.
> Every ambiguity the issue left open was raised in `00-overview.md` / the original
> `OPEN_QUESTIONS.md`.
> All of those questions are now resolved. Their resolutions are folded into §7/§8
> below, marked with pointers, so nothing silently disappears (§11).

## 1. Purpose / origin

Issue #33 is a one-line brief. For a small, bugfix-shaped PR, the pass attributes each
changed line to the commit that introduced it. It finds that commit's PR. It checks
whether that PR carried a review comment on those lines. If an AI backend judges that
the comment would have prevented the bug now being fixed, the pass posts a comment on
the current PR that links back to it. This requirement is not derived from a reference
implementation. It turns an observation (review comments get raised and then silently
ignored) into a mechanical check.

This pass is the third PR-level pass added to `tools/pr-review`'s existing
`self-review` / `review-requested` runners. It comes after the design-review pass
(`design-review-requirements.md`, dotharness#3) and the requirement-traceability pass
(`requirement-traceability-requirements.md`, dotharness#4). Those two documents, and
the code they produced, are the primary reference implementation for this document.
This document reuses that shape rather than inventing a new one: the
`run_pr_level_pass` orchestration skeleton, the per-pass completion-marker
convention, and the `self_review.py`/`review_requested.py` wiring shape.

According to dotharness#21's decisions log, this pass is explicitly **not** a
subprocess/JSON integration with a separate tool the way `[vibe_heal]` is. Issue #33
only asks the pass to *mirror that block's config style* (an `enabled`-gated
`.harness.toml` table). The pass itself lives in-process in `harness/runners/`,
alongside `self_review.py` and `review_requested.py`. Dotharness#21 also flags the
git-blame/diff/log-parsing plumbing as a second candidate for a future shared library
(alongside vibe-heal's `git/` and code-health's churn loader). That extraction is
**deferred until a third consumer needs it, not built here.**

## 2. Problem statement

Today, `self-review` and `review-requested` run three passes per PR. The first is
per-file correctness review, which also produces a PR-level summary
(`review-file.md` and `review-summary.md` together). The second is design review
(`review-design.md`). The third is requirement-traceability review
(`review-traceability.md`). None of them look backward in time. A reviewer's comment
on the original PR that introduced a bug — "this edge case isn't handled," "this will
break if X" — is either addressed then, or it silently disappears into review history
that nobody re-reads. When the predicted bug eventually surfaces and gets fixed in a
later, unrelated PR, no mechanism connects the fix back to the comment that would
have prevented it. The person who ignored (or missed) that comment gets no signal
that this specific class of oversight is recurring. A different author fixing the same
code has no way to discover the earlier warning at all.

This is a distinct failure mode from what the other three passes already catch.
Correctness review only looks at the *current* diff in isolation. Design review and
traceability review only look at the current PR's relationship to its own linked
ticket. None of them looks at a file's history for a relevant, unaddressed comment.

## 3. Goals

- **G1.** The pass runs on every small, bugfix-shaped PR in `self-review` or
  `review-requested` (§7.3, §7.4). For every changed line in the PR's diff, it finds
  the commit that most recently touched that line's *prior* content before this PR's
  fix. It also finds the PR that contains that commit (§7.1).
- **G2.** For each such introducing PR, the pass fetches its review comments. It
  selects the comments whose (path, line) falls within the same diff hunk as the
  blamed change (§7.2). The match uses the resolved tolerance model, not
  exact-line-only matching.
- **G3.** For each candidate comment from G2, the AI backend judges whether
  addressing it, at the time it was made, would have prevented the bug the current PR
  now fixes. Only a positive judgment produces a finding (§7.2).
- **G4.** The pass posts at most one PR-level comment on the current PR per run. It
  lists every confirmed regret finding. It links each finding to its original
  comment: the comment's PR number and, where resolvable, its direct GitHub URL. A
  completion marker gates the pass, so a rerun never reposts (§7.6).
- **G5.** The pass runs as part of `self-review` and `review-requested` whenever
  `[regret_review].enabled = true` in `.harness.toml`. It is the first PR-level pass
  in this codebase with its own on/off toggle. Design review and traceability review
  have no toggle. They always run (§7.5, §9).
- **G6.** The pass never runs on a PR whose total changed-line count exceeds
  `[regret_review].max_diff_lines` (default 50). This is the issue's "≤N lines,
  configurable" requirement (§7.1, §7.5).
- **G7.** All git-blame, PR-lookup, and comment-matching code lives inside this
  pass's own module. No shared library is extracted for it in this effort (§4,
  dotharness#21).

## 4. Non-goals

- **Not wiring into `review-prs`.** This matches design review's non-goal
  (`design-review-requirements.md` §4). `review-prs` uses a structurally different
  pipeline (the `[vibe_heal]` integration). It has no existing per-file/summary/
  design/traceability pass to sit alongside. This decision was confirmed for this
  effort, not inherited by default (resolved via the project's own open-questions
  checkpoint, §11.1).
- **Not judging the correctness of the current PR's fix.** That is correctness
  review's (`review-file.md`) job. `regret-review` only judges the *connection*
  between an old comment and the new fix — never whether the fix itself is adequate.
- **Not a general "stale review comment" auditor.** It never scans old PRs on its own.
  It only looks at comments on lines that a *current, small, bugfix-shaped* PR is
  touching right now.
- **Not building a shared git-blame/diff/log-parsing library.** According to
  dotharness#21's decisions log, this pass implements its own blame, commit→PR, and
  comment-matching plumbing directly. Vibe-heal's `git/` and code-health's churn
  loader work the same way today. Extraction is explicitly deferred until a third
  real consumer needs it.
- **Not extending to non-GitHub-native history.** If the introducing commit can't be
  resolved to any PR (direct push to the base branch, deleted PR, private/renamed
  repo), the pass silently skips that line. It logs no error. It never surfaces the
  skip to a human (§7.1, resolved).
- **Not adding a new git-blame-based UI, dashboard, or report** beyond the single PR
  comment G4 describes. It adds no standalone CLI command and no aggregate "regret
  rate" metric.

## 5. Glossary

- **Current PR** — the PR a `regret-review` run is evaluating right now. Its diff is
  "the bug now being fixed."
- **Introducing commit** — the commit that `git blame` attributes a changed line's
  *prior* content to. Blame the base-branch version of the lines the current PR's
  diff removes or modifies. Never blame the new lines the fix adds. Blaming the
  fix's own added lines would only point at the current PR's own commit.
- **Introducing PR** — the pull request that GitHub's API reports as containing the
  introducing commit. Look it up with `GET /repos/{owner}/{repo}/commits/{sha}/pulls`
  (exposed via `gh api repos/{owner}/{repo}/commits/{sha}/pulls`). It was chosen over
  commit-message parsing because it works regardless of merge/squash/rebase strategy.
- **Diff hunk** — a contiguous region of change, delimited by unified-diff
  `@@ -a,b +c,d @@` markers. This pass uses it as the tolerance boundary when
  matching a review comment's (path, line) against a blamed change's location
  (§7.2). It is not "the whole file."
- **Regret finding** — a case with two conditions. The introducing PR carried a
  review comment in the same diff hunk as the introducing change. The backend judges
  that addressing the comment back then would have prevented the bug the current PR
  fixes.
- **Regret comment** — the PR-level comment that `regret-review` posts on the current
  PR. It carries every confirmed regret finding for that run. It is marked with its
  own completion marker, mirroring `INLINE_REVIEW_MARKER` / `DESIGN_REVIEW_MARKER` /
  `TRACEABILITY_REVIEW_MARKER` in `harness/runners/common.py:24-26`.
- **Bugfix-shaped PR** — a PR whose total changed-line count is at or below
  `[regret_review].max_diff_lines`. The count sums added + removed lines across every
  changed file. `build_file_review_section` (`harness/runners/common.py:1130-1160`)
  already uses this same measure per-file.
- **PR-level pass** — one backend invocation per PR, given the whole PR's diff and
  file set as context, as opposed to a per-file pass. The term is defined in
  `design-review-requirements.md` §5.

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

The pass works on the current PR's diff against its base branch. It uses the same
`origin/{base}...HEAD` comparison that `get_changed_files`/`get_file_diff` already
use (`harness/runners/common.py:1086-1118`).

1. **Bugfix-shaped gate, checked first.** The pass sums added + removed lines across
   every changed file. It uses the same counting rule as `build_file_review_section`'s
   diff-line count (now the shared `count_diff_lines` helper,
   `harness/runners/common.py:1121-1127`). If the total exceeds
   `config.regret_review.max_diff_lines`, the pass skips this PR entirely: no blame,
   no backend call, no comment. This is a cheap, early exit.
2. **Per file, per removed/modified line range:** the pass runs `git blame` against
   the pre-fix state. It blames `origin/{base_branch}` (or the PR's merge-base with
   it) for the file. The blame is restricted to the line ranges that the current
   PR's diff hunks mark as removed or changed. The command is
   `git blame -L <start>,<end> <base_ref> -- <path>`, run once per contiguous
   removed/changed range per file. Newly *added* lines with no prior content are not
   blamed. There is nothing to trace back.
3. **Deduplicate** by introducing commit SHA within a single run. A range of lines
   that all blame to the same commit becomes one candidate. The pass does not
   re-process the same commit repeatedly. One file may plausibly hold many small
   hunks that all blame to the same earlier commit.
4. **Resolve each unique introducing SHA to its PR(s)** via `gh api
   repos/{owner}/{repo}/commits/{sha}/pulls`. If the call returns zero PRs (direct
   push, deleted PR, or the call fails or is rate-limited), the pass **skips that
   commit's lines silently**. It logs no error above debug level. It produces no
   partial finding. This matches the fail-open convention that every existing helper
   in `harness/runners/common.py` follows for inconclusive lookups (e.g.
   `list_open_prs_matching_authors`, `resolve_linked_tickets`). If the call returns
   more than one PR (e.g. cherry-picked across branches), the pass evaluates every
   returned PR independently. A comment on any one of them is a valid candidate.

### 7.2 Comment matching and backend judgment

1. **Fetch the introducing PR's comments** with the existing `fetch_pr_comments`
   shape (`harness/runners/common.py:1250-1294`). That function shells out to
   `scripts/pr-comments.py fetch --pr N`. It accepts an arbitrary PR number via
   `gh pr view N`. It is not restricted to the current branch's PR. This was verified
   directly against `scripts/pr-comments.py`'s `cmd_fetch`. The pass needs no new
   fetch mechanism. It only points the existing call at the introducing PR's number
   instead of the current one. Only `inline`/`review`-typed comments carry a
   `path`/`line` (`common.py`'s `fetch_pr_comments` docstring). `issue`-typed
   comments have neither. They are never candidates for a location match.
2. **Hunk-tolerance match, not exact-line match (resolved, §11.2).** A fetched
   comment is a candidate if two things hold. Its `path` matches the blamed file. Its
   `line` falls within the same contiguous diff hunk that the introducing commit's
   change occupied — in the introducing PR's own diff, at the commit the comment was
   posted against. The match is not only the exact blamed line. The hunk tolerance
   absorbs drift from force-pushes or later commits within that same PR. It still
   requires the same localized region of change, not a whole-file match. To compute
   "the same diff hunk," the pass diffs the introducing commit against its own
   parent, restricted to the blamed file. It takes that hunk's `@@` line range as the
   match window. It never uses the current PR's hunks, which are a different diff
   entirely.
3. **One backend invocation per current-PR run, not per candidate.** The pass mirrors
   `_run_design_review`/`_run_traceability_review`'s one-call-per-PR shape
   (`harness/runners/self_review.py:235-272`). It batches every candidate
   `(comment,
   blamed_change)` pair found across every file in the current PR into a
   single prompt. The prompt gives the backend the current PR's fix diff. It builds
   that diff via `build_file_review_section`
   (`harness/runners/common.py:1130-1160`), the same shape every other pass already
   uses. Per candidate, the prompt also gives the original comment's body, its
   author, and the diff of the change it was left on. The backend returns which
   candidates (if any) it judges as genuine regrets. This pass does not parse
   free-form prose out of a plain-text response. The prompt must ask for a structured
   answer, e.g. one candidate per line. The poster (§7.6) parses that answer
   deterministically before treating anything as confirmed.
4. **No candidates found (either no blame hits, or blame hits but no PR/comment
   match).** The pass performs no backend invocation and posts nothing. This mirrors
   traceability review's "no linked ticket" short-circuit
   (`harness/runners/common.py:743-769`) in spirit: skip the backend call when there
   is structurally nothing to judge. The two skips differ. Regret-review's silent
   skip ends only *this run*. It is not permanently marked done the way "no linked
   ticket" is. A later PR update could touch different lines with different blame
   history.

### 7.3 Wiring into `self-review`

This wiring follows `_run_design_review`'s exact shape
(`harness/runners/self_review.py:235-272`):

- This runner adds a new `_run_regret_review` function. `_run_locked`'s per-PR loop
  invokes it (`harness/runners/self_review.py:575-584`), alongside the existing
  `run_design`/`run_traceability` calls. The call is gated additionally on
  `config.regret_review.enabled` (§7.5). When that flag is `false`, the pass is never
  invoked at all. It is not merely skipped per-PR. Design and traceability review
  have no such config gate at all (§9).
- The pass keeps its own persisted state: `regret_reviewed_prs`. It mirrors
  `design_reviewed_prs`' read/write/prune shape (`harness/state.py`'s
  `prune_self_review_state`/`add_design_reviewed_pr` equivalents). The state is
  tracked independently. This pass's retry never forces, or is forced by, the
  per-file review's or design review's retry behavior. `_run_design_review`'s
  docstring gives the same rationale.
- The orchestration itself (noop-if-done → build prompt → run backend → re-verify
  marker) reuses `common.run_pr_level_pass` (`harness/runners/common.py:772-836`)
  unchanged. The pass adds no new orchestration skeleton.
- The pass has its own instructions file, `review-regret.md`. It is read from the
  same `knowledge_dir / "pr-review"` directory as the other three
  (`harness/runners/self_review.py:606-613`). This effort authors it by hand (it is
  not scaffolded automatically). The instructions file's prose content is this
  project's own deliverable. It follows `review-design.md`'s existing structure and
  audience as a model.

### 7.4 Wiring into `review-requested`

This wiring follows `_run_design_review`'s exact shape in this runner
(`harness/runners/review_requested.py:357-423`):

- This runner adds a new `_run_regret_review` function. It sits alongside the
  file/summary/design/traceability calls in this runner's per-PR loop
  (`harness/runners/review_requested.py:248-258`). It is gated the same way as §7.3,
  on `config.regret_review.enabled`.
- **No persisted state**, exactly like design and traceability review in this runner
  (`review_requested.py:369-372`). `has_comment_fn` (the regret-comment marker check)
  is this pass's only "already done" signal here. `review_requested` has no state
  file at all.
- The pass uses the same `review-regret.md` instructions file and the same
  `run_pr_level_pass` reuse.

### 7.5 `.harness.toml` config schema

The pass adds a new `RegretReviewConfig` dataclass in `harness/config.py`. It is
added to `HarnessConfig` the same way `VibehealConfig`/`FocusedReviewConfig`/
`AddressCommentsConfig` already are (`harness/config.py:36-58, 89-95`). The config
is parsed from a `[regret_review]` table, following `harness/config.py:109-217`'s
`load_config` pattern:

```python
@dataclass
class RegretReviewConfig:
    enabled: bool = False
    max_diff_lines: int = 50
    authors: str | list[str] = "*"
    regret_review_timeout: int = 300
```

- `enabled` — off by default, per G5/§9. This is the only toggleable pass in this
  codebase.
- `max_diff_lines` — the bugfix-shaped threshold (§7.1 step 1). The default is 50.
  That is a round, conservative starting point for "small fix." It is pending real
  usage data. No existing corpus can derive it empirically, unlike
  `docs/requirements.md`'s sizing bands elsewhere in this monorepo. The default is
  flagged as a starting point rather than invented as if measured.
- `authors` — mirrors `VibehealConfig.authors` (`harness/config.py:37-46`). It
  accepts `"*"` (all authors) or an explicit allowlist of GitHub usernames. It
  applies to the **current** PR's author. The introducing PR's author is never
  filtered. The point is tracing *whoever's* old comment was ignored.
- `regret_review_timeout` — a per-PR wall-clock budget. It covers this pass's own
  git/`gh` calls (blame, commit→PR lookups, comment fetches) plus the single backend
  invocation. It is separate from `harness.backend_timeout_seconds`. This pass can
  issue several sequential `gh api` calls per PR before it reaches the backend.

`docs/configuration.md`'s schema tables need a new `[regret_review]` section. It
mirrors the existing `[vibe_heal]`/`[focused_review]` entries there. The section
lands once this shape is implemented.

### 7.6 Regret comment format, marker, and idempotency

- The pass adds a **new marker constant** in `harness/runners/common.py`, alongside
  `INLINE_REVIEW_MARKER`/`DESIGN_REVIEW_MARKER`/`TRACEABILITY_REVIEW_MARKER`
  (`common.py:24-26`): e.g. `REGRET_REVIEW_MARKER = "<!-- osc-review-regret -->"`.
- **Idempotency and flagged-locations helpers** reuse the existing generic
  `has_pr_level_pass_comment`/`check_pr_level_pass_comment_status`
  (`common.py:374-393`) and `get_pr_level_flagged_locations`
  (`common.py:446-471`). Those helpers are already parameterized by `marker`. The
  pass needs no per-pass duplicate of them. It adds only thin
  `has_regret_review_comment`/`check_regret_review_comment_status` wrappers,
  mirroring `has_design_review_comment`/`check_design_review_comment_status`
  (`common.py:400-410`).
- **Comment body**: one PR-level comment, never inline. A regret finding's
  "location" is in the *introducing* PR, not the current one. An inline comment on
  the current PR's diff would anchor it to the wrong place. The comment lists every
  confirmed regret finding for this run. For each finding it gives: the file/region,
  the introducing PR number, a link to the original comment, and a short quote of the
  original comment body. The link is constructed from the PR number + comment id, the
  same way `fetch_pr_comments`'s cached shape already carries them.
  `scripts/pr-comments.py`'s fetched comment dicts include enough to build a direct
  GitHub URL. The comment ends with `REGRET_REVIEW_MARKER`.
- **A rerun that finds new regret candidates on an already-regret-commented PR**
  behaves like every other PR-level pass here. `is_done`/`has_comment_fn` gate
  whether the backend runs at all. Once a regret comment has been posted for this
  PR, this pass does not re-run for it in this runner cycle. No "append new findings
  to an existing comment" mechanism is in scope (see §9).

## 8. Non-functional requirements

- **Cost bound follows directly from G6.** The `max_diff_lines` gate (default 50)
  bounds this pass's per-PR cost. The cost is: a bounded number of `git blame`
  calls, one `gh api commits/
  {sha}/pulls` call per unique introducing commit, one
  `pr-comments.py fetch` per unique introducing PR, and at most one backend
  invocation. The cost never scales with a large PR. The pass either exits at step 1
  of §7.1 or operates on a small, bounded line count. No empirical corpus exists yet
  to derive a tighter number than the round default above. The document flags this
  rather than inventing it as if measured (§7.5).
- **Its own bounded per-PR timeout**: `regret_review_timeout` (§7.5, default 300s).
  It covers every git/`gh` call this pass makes plus its single backend invocation.
  It is separate from `harness.backend_timeout_seconds`, which only bounds the
  backend call itself.
- **Rate-limit awareness.** `gh api commits/{sha}/pulls` and the comment fetch are
  both authenticated GitHub API calls. They are subject to the same rate limits that
  every other `gh`-based helper in `harness/runners/common.py` already operates
  under (`TIMEOUT_GH = 30`, `common.py:16`). The pass introduces no new
  rate-limit-specific handling beyond the existing fail-open pattern (§7.1 step 4).
  A rate-limited lookup is treated the same as "no PR found." The pass does not
  retry it within the same run.
- **No new external dependency.** Every mechanism here (`git blame`, `gh api`, the
  existing `pr-comments.py` script, the existing `Backend` abstraction) is already
  present in this codebase. This pass adds no new subprocess tool, package, or
  service.

## 9. Out-of-scope / explicit exclusions

- **Not toggle-free like design/traceability review.** Those two passes run
  unconditionally. This one is explicitly config-gated (G5). Unlike them, it has real
  per-run cost variance (blame + multiple `gh api` calls). A repo owner may
  reasonably want it off by default. This is a deliberate asymmetry with the existing
  two passes, not an oversight.
- **Not appending to, or editing, a previously-posted regret comment** if a later run
  on the same PR finds additional candidates (§7.6). If the marker is already
  posted, a rerun is a full noop for this pass. This matches design and
  traceability review today.
- **Not attempting fuzzy content-based line matching** (e.g. matching moved/renamed
  code by diffing content rather than following git's own rename detection). The
  pass uses only `git blame`'s own commit attribution and the introducing PR's own
  diff hunks. If `git blame` itself loses the line across a rename it doesn't
  follow, that line is simply not traced. No additional heuristic is layered on top
  in this effort.
- **Not building the requirement-traceability pass's early-comment-window pattern**
  (`TRACEABILITY_COMMENT_WINDOW_SECONDS`, `common.py:41-43`) into this pass. That
  window is specific to catching clarifying comments posted right after a PR opens.
  It has no analog here. This pass looks at all of an old PR's review comments, not a
  time-boxed window of its issue comments.

## 10. System/tool shape

- **Location:** a new module, `harness/runners/regret_review.py`, alongside
  `self_review.py`/`review_requested.py`/`review_prs.py` in
  `tools/pr-review/harness/runners/`. The shared plumbing (blame, commit→PR
  resolution, hunk matching) lives as private helpers in this same module, per
  §4/G7. It is not extracted into `harness/runners/common.py` or a new top-level
  package. Only this one module uses it. Both `self_review.py` and
  `review_requested.py` call the pass, but the *implementation* of the blame/lookup
  logic has one owner.
- **Config:** `RegretReviewConfig` in `harness/config.py` (§7.5), parsed as part of
  the existing `.harness.toml` `load_config` (`harness/config.py:109-217`).
- **Instructions file:** `review-regret.md`, added to the same knowledge directory
  where the other three prompt templates already live. The runner reads it via
  `config.harness.knowledge_dir
  / "pr-review"` (e.g. `~/.harness/knowledge/pr-review/review-regret.md`). It
  follows
  `review-design.md`'s structure and audience as a model (§7.3).
- **New marker:** `REGRET_REVIEW_MARKER` in `harness/runners/common.py` (§7.6).
- **Docs:** `docs/configuration.md` gets a new `[regret_review]` section (§7.5).
  This document itself (`regret-review-requirements.md`, or wherever this project's
  `.projects/regret-review/requirements.md` is ultimately checked in) becomes the
  sibling of `design-review-requirements.md` /
  `requirement-traceability-requirements.md` at the repo root, once approved.
- **Tests:** unit coverage for the blame/hunk-matching logic. The tests are
  fixture-based. They do not hit real `gh` or `git blame` against GitHub. They mirror
  the existing test structure for `self_review.py`/`review_requested.py`/`common.py`
  in `tools/pr-review/tests/`.

## 11. Decisions log (resolved via this project's own open-questions checkpoint)

1. ~~**Which runner command(s) does this pass hook into?**~~ **Resolved:** both
   `self-review` and `review-requested` (§7.3, §7.4) — not `review-prs` (§4). The
   pass is scoped to PRs a human is actually paying attention to right now. This
   matches where design review and traceability review already run. It avoids opening
   a new integration point.
2. ~~**Line-range matching strictness under drift** (force-pushes, renames, later
   commits within the same introducing PR)~~. **Resolved:** same-diff-hunk
   tolerance, not exact file+line (§7.2 step 2, glossary "Diff hunk"). It was chosen
   over exact-line matching to absorb realistic drift while still requiring the same
   localized region of change, not a whole-file match.
3. ~~**`[regret_review]` config shape beyond `enabled`.**~~ **Resolved:**
   `max_diff_lines` (default 50), `authors` (mirrors `VibehealConfig.authors`), and
   `regret_review_timeout` (default 300s) — see §7.5 for the full dataclass and
   rationale for each field's default.
4. ~~**Behavior when the introducing commit/PR can't be resolved** (deleted PR,
   direct push, private/renamed repo).~~ **Resolved:** the pass silently skips that
   commit's lines. No error is surfaced. This is consistent with every existing
   fail-open helper in `harness/runners/common.py` (§7.1 step 4, §4).

No further open decisions remain from the brief. Any question raised for the first
time during `decompose`/`draft-phases` (e.g. exact hunk-matching algorithm
implementation detail, `review-regret.md`'s precise prose) should be logged fresh at
that stage. It should not be assumed to be covered here.

## 12. Next step

Once this document is reviewed and approved, the next step is
`spec-prism-flow plan decompose` against this `requirements.md`. The command splits
§7's six functional areas (7.1–7.6) into a dependency-ordered decomposition tree.
Then `plan draft-phases` produces one phase file per leaf: config/dataclass,
blame+commit→PR resolver, comment-fetch+hunk-match, backend-judgment prompt +
`review-regret.md` authoring, `self-review` wiring, `review-requested` wiring,
marker/idempotency helpers, `docs/configuration.md` update. `plan review` is the exit
gate before `build run` starts opening PRs.
