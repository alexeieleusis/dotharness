# Requirements: Dedicated Design/Architecture Review Pass

> Drafted per `~/.harness/tools/requirements-doc-drafting-prompt.md`, from
> [dotharness#3](https://github.com/alexeieleusis/dotharness/issues/3). This is the
> single source of truth a later, smaller work order (a phase file / ticket) will quote
> from — not the work order itself. Every ambiguity the issue left open was raised in
> the original decisions log and has since been resolved (§11) — resolutions are folded
> into §7/§8 and marked with pointers so nothing silently disappears.

## 1. Purpose / origin

`dotharness` (this repo) already runs an automated, per-file, correctness-focused code
review (`review-file.md`) plus a one-shot PR summary (`review-summary.md`), wired into
two runners: `self-review` (the author's own open PRs) and `review-requested` (PRs
where GitHub review was explicitly requested from the harness's `gh` account). Issue #3
asks for a third, PR-level pass that looks at design/architecture concerns — wrong
abstraction, wrong module boundaries, over- or under-engineering — which the
correctness pass is not meant to carry. This document generalizes that one-line issue
into a precise enough spec to hand to a phase file. It is not derived from a reference
implementation; it is invented from the issue text plus the conventions already
established by `review-file.md`/`review-summary.md` and their two call sites.

Issue #4 (requirement-traceability pass) is explicitly called out by #3 as needing
*joint* design, because both are "PR-level, single-invocation passes" that must not
multiply `self-review`'s existing per-file retry-on-failure cost. This document is
scoped to #3 only; §7.4 records what was decided about anticipating #4 without building
it now.

## 2. Problem statement

Today, the only automated checkpoint before a human reviews a PR is
`review-file.md`, which explicitly reviews one file's diff at a time against four
dimensions: Logic & Correctness, Performance & Bottlenecks, Maintainability, Security
(`knowledge/pr-review/review-file.md:19-27`, at the `dotharness` repo root). Two things
follow from that shape:

- **Design flaws are structurally invisible to it.** A per-file prompt is shown one
  file's diff in isolation; it cannot see that a PR introduced a second implementation
  of an existing abstraction in another file, or put a concern in the wrong module,
  because that judgment requires seeing the PR's file set as a whole.
- **The one dimension that gestures at design ("Maintainability — code smells,
  DRY/SOLID violations") is bolted onto a P0/P1-severity, per-file bug-finding pass.**
  That pass's output contract ("If there are no P0/P1 findings for this file, post
  nothing") and tone (skeptical senior-dev bug-hunting) is a poor fit for
  cross-file, often-debatable design judgment calls, and overloading it risks either
  drowning design feedback in bug-severity framing or suppressing it entirely under the
  P0/P1 bar.

Without a dedicated pass, the *last* realistic point to catch a wrong abstraction or
module boundary is human code review — after the PR is already built, at the point
where undoing the design choice is most expensive.

**Resolved (§11.6):** this pass is additive only. `review-file.md`'s existing four
dimensions, including "Maintainability," are unchanged by this effort — see §4, §9.

## 3. Goals

- **G1.** Every PR run through `self-review` or `review-requested` gets exactly one
  additional, PR-level (not per-file) automated pass whose sole focus is
  abstraction/module-placement/over- or under-engineering — never correctness, security,
  or performance findings already owned by `review-file.md`. It runs unconditionally,
  the same way the existing file/summary passes do — no opt-in config flag (§11.8, §7.1,
  §8).
- **G2.** The new pass's prompt template is a standalone file, analogous in structure,
  location convention, and audience to the existing `review-file.md` /
  `review-summary.md` templates, so it can be authored, edited, and versioned the same
  way (§7.1).
- **G3.** The new pass integrates into both `self-review` and `review-requested`
  without changing either runner's existing idempotency guarantee: re-running the
  command against an already-fully-reviewed PR must not re-post duplicate design
  comments, exactly as file/summary comments aren't reposted today (§7.2, §7.3).
- **G4 (resolved, §11.1).** The design pass's completion is tracked **independently**
  from the existing per-file/summary retry state. A design-pass failure never forces
  re-running file reviews, and a file/summary failure never forces re-running (or
  blocks) the design pass. This keeps the pass's added cost to at most one extra
  invocation per attempt, and often zero once it has already succeeded once, regardless
  of what else is failing/retrying that cycle (§7.2, §7.3).
- **G5.** The pass's findings are traceable to the specific file(s)/area(s) they concern
  well enough that a human reading them can locate the code in question — satisfied
  directly by posting file/line-anchored inline comments for file-specific findings,
  alongside one PR-level comment for cross-cutting ones (§11.3, §7.1).

## 4. Non-goals

- **Not touching `review-file.md` at all (resolved, §11.6).** No dimension is trimmed,
  narrowed, or reworded as part of this effort. Overlap between the two passes (e.g.
  both potentially reacting to a DRY violation, one as a correctness finding, one as a
  design finding) is accepted as a known tradeoff for this iteration, not a defect to
  fix here — see §9.
- **Not wiring into `review-prs`.** `review-prs` reviews *other* people's PRs via the
  separate `vibe_heal` pipeline and does not use `review-file.md`/`review-summary.md` at
  all (`docs/commands/review-requested.md:97-99` and the `[vibe_heal]` config section).
  Issue #3 only names `self-review` and `review-requested`; `review-prs` is out of
  scope, considered and rejected because it's a structurally different pipeline with no
  existing per-file/summary pass to sit alongside.
- **Not implementing issue #4 (requirement-traceability pass).** #4 is explicitly a
  separate issue with its own ticket-resolution mechanics (Linear/Jira/GitHub Issues
  convention, scope-creep/gap detection).
- **Not building a generic "PR-level pass" framework now (resolved, §11.5).** Even
  though #3 and #4 are both instances of the same shape ("PR-level, single-invocation
  pass with its own idempotency marker, fate decoupled from the file-review retry
  loop"), no shared abstraction is extracted in this effort. Instead, every place in the
  new code that a future generalization would touch must carry an explicit code comment
  naming the reuse opportunity, so #4 doesn't have to rediscover them (§7.4).
- **Not adding a config flag or `[design_review]` section (resolved, §11.8).** The pass
  runs unconditionally whenever `self-review`/`review-requested` run, the same way
  `review-file.md`/`review-summary.md` aren't independently toggleable today. No new
  `harness.toml` schema surface.
- **Not changing `review-requested`'s or `self-review`'s file-level review dimensions,
  output format, or existing marker conventions** (`<!-- osc-review-inline -->`, the
  `[bot]osc-review`/`Review Summary` comment-prefix checks) — the new pass gets its own,
  additive marker instead of reusing or altering these (§11.4, §7.1).

## 5. Glossary

- **PR-level pass** — a review pass invoked once per PR (one backend call), given
  context spanning the whole PR's diff/file set, as opposed to a **per-file pass**
  (one backend call per changed file, e.g. `review-file.md`).
- **Backend** — the AI coding-agent CLI (`opencode` or `claude`) that `harness` shells
  out to via `harness/backend.py:Backend.run()`. A single `Backend.run()` call is one
  **invocation**.
- **Knowledge dir** — `harness.knowledge_dir` (default `~/.harness/knowledge`), the
  directory the runners read the actual prompt template files from at execution time
  (`pr-review/review-file.md`, `pr-review/review-summary.md`, etc.). In this repo the
  default path resolves to this very checkout's own `knowledge/` directory (this repo
  is cloned to `~/.harness`), so — for this repo — these templates are git-tracked,
  versioned, reviewed content, not personal scratch files; `knowledge_dir` is still a
  configurable path, so a *different* installation could point it elsewhere, but that's
  a deployment concern, not a property of the templates themselves. This is distinct
  from `docs/commands/*.md` in this repo, which document runner *behavior*, not prompt
  *content*.
- **Design finding** — an output of the new pass: a P0/P1-level judgment about
  abstraction choice, module/layer placement, or over-/under-engineering, as opposed to
  a **correctness finding** (P0/P1 bug, owned by `review-file.md`).
- **Marker** — a literal string a runner searches for in a PR's existing GitHub comments
  to detect "this bot already did X here." Existing examples: `INLINE_REVIEW_MARKER =
  "<!-- osc-review-inline -->"`, and the `Review Summary`/`osc-review` substring check
  in `is_review_summary_comment` (`harness/runners/common.py:21-22,254-256`).
- **`DESIGN_REVIEW_MARKER`** — the new marker this effort introduces (resolved, §11.4):
  `"<!-- osc-review-design -->"`, appended to every comment the design pass posts,
  whether inline or PR-level (§7.1).

## 6. Feature/component breakdown

| Component | Question it answers | Shape | Input | Output |
|---|---|---|---|---|
| `review-design.md` prompt template | What counts as a design finding, and how should it be posted? | Markdown instructions file in the knowledge dir (§7.1), git-tracked at `knowledge/pr-review/review-design.md` | PR metadata, full changed-file list, each file's diff (concatenated per-file review sections, PR-wide), PR description, optional extra-knowledge/vibe-heal context | Always exactly one PR-level `gh pr comment` (findings summary or "no blocking issues"), plus zero or more inline `gh api .../comments` for file-specific P0/P1 findings — all carrying `DESIGN_REVIEW_MARKER` |
| `self_review.py` wiring | When does `self-review` run the design pass, and how does its outcome affect state? | New, independent step in `_process_single_pr` | Same `ctx` dict already built per PR | Success/failure tracked in a **new, independent** `self_review.json` field (`design_reviewed_prs`), decoupled from `reviewed_prs`/`partial_reviews` (§7.2) |
| `review_requested.py` wiring | Same question, for the no-persisted-state runner | New, independent step in `_process_pr` | Same per-PR context already gathered | Gates `remove_reviewer` (design failure/timeout blocks removal); skipped as a noop (no re-invocation) if `DESIGN_REVIEW_MARKER` is already found on the PR (§7.3) |
| Idempotency marker/detection helper | How does a re-run know the design pass already happened for this PR revision? | New constant (`DESIGN_REVIEW_MARKER`) + `is_design_review_comment`/`has_design_review_comment` in `harness/runners/common.py`, mirroring `is_review_summary_comment`/`has_review_summary_comment` | A GitHub comment body | Boolean "already done" signal, used by both runners; code comments at the reuse-relevant points flag it as a future generalization candidate for issue #4 (§7.4, §11.5) |

## 7. Detailed functional requirements

### 7.1 The `review-design.md` prompt template

- **Location:** `knowledge/pr-review/review-design.md` at the `dotharness` repo root —
  a new git-tracked file, added and reviewed the same way `review-file.md` and
  `review-summary.md` already are, in the same PR as the runner code changes below.
  `harness validate` only checks that `knowledge_dir` itself exists
  (`harness/cli.py:105-106`), never that specific template files inside it exist; that
  stays true after this addition, and is only a concern for an installation whose
  `harness.knowledge_dir` points somewhere other than a checkout of this repo (e.g. a
  fork or a hand-picked directory) — such an installation would need to copy this file
  in manually, same as it already must for `review-file.md`/`review-summary.md` today.
- **Scope of judgment**, explicitly restated from the issue and Non-goals (§4): wrong
  abstraction, wrong module/layer/package placement, over-engineering (unneeded
  indirection, premature generalization, unnecessary configurability), and
  under-engineering (missing an abstraction the change clearly calls for, duplicated
  logic that should have been unified). Explicitly **not**: correctness bugs, security,
  performance, or style/lint-level nits — those stay with `review-file.md`, unchanged
  (§4, §11.6).
- **Tone and framing** should follow the same "Tone"/"Perspective"/"Role" preamble
  pattern already established in `review-file.md` (professional, constructive, assume
  competence, frame as questions/suggestions) so the two passes read as one coherent
  reviewer persona to a human reading the PR, not two disjoint tools.
- **Severity gate (resolved, §11.7): P0/P1 only**, using the same two-tier bar as
  `review-file.md` ("For each P0 (critical) or P1 (high priority) finding..."), applied
  to design judgment instead of correctness bugs — e.g. P0 for an abstraction/placement
  choice that will actively cause defects or major near-term rework, P1 for
  over-/under-engineering that meaningfully hurts maintainability. This is not an
  open-ended "post every stylistic opinion" pass; the exact P0/P1 line for design
  findings is drafted into `review-design.md` itself, not left implicit.
- **Required inputs to the prompt** (built by the runner, appended after the template
  body — mirroring `_build_file_review_prompt`/`_build_file_prompt`'s existing
  concatenation pattern):
  - PR URL, PR number, repo name, head commit SHA.
  - The PR description (`get_pr_description`).
  - The full list of changed files.
  - **Per-file diff content, concatenated across every changed file** (resolved,
    §11.3) — i.e. `build_file_review_section` output for each file, joined into one
    PR-wide prompt, the same way each per-file `review-file.md` invocation already
    builds its single-file section. This is required because inline findings need
    enough line-level detail for the model to anchor a comment to a specific line;
    unlike `review-summary.md` (which only lists file names), this prompt cannot rely on
    the backend re-deriving diffs itself.
  - Optional `harness.review_knowledge_file` extra-guidance section and vibe-heal static
    analysis context, exactly as already appended to every other prompt in both
    runners, for consistency.
  - **Prior `DESIGN_REVIEW_MARKER` inline comments already posted on this PR** (added in
    review; see the partial-failure note under Output contract below) — fetched via
    `repos/{REPO}/pulls/{PR_NUMBER}/comments`, filtered to those whose body contains
    `DESIGN_REVIEW_MARKER`, and passed to the prompt as a list of `(file, line)` pairs
    already flagged, with an instruction not to re-post a finding for any pair already in
    that list. Unlike the PR-level noop checks in §7.2/§7.3, this fetch runs on *every*
    invocation, not just when deciding whether to invoke at all — that's what lets it
    catch a partial prior attempt.
- **Output contract (resolved, §11.2/§11.3/§11.4), stated precisely enough to be quoted
  verbatim into the actual template:**
  1. **Inline findings** (file-specific, P0/P1 only): posted exactly like
     `review-file.md`'s inline comments —
     ```
     gh api repos/{REPO}/pulls/{PR_NUMBER}/comments \
       -f body="...<!-- osc-review-design -->" -f commit_id="{COMMIT}" -f path="..." -F line=<N>
     ```
     with `DESIGN_REVIEW_MARKER` (`<!-- osc-review-design -->`) in place of
     `INLINE_REVIEW_MARKER`, so the two passes' inline comments are distinguishable by
     marker even though they use the same GitHub endpoint.
  2. **One PR-level comment, always posted exactly once per successful run** (this is
     the "both" resolution to §11.3 — a design pass can have cross-cutting observations
     that don't anchor to one file/line, and this comment is also the reliable
     idempotency signal described in §7.3):
     ```
     gh pr comment {PR_NUMBER} --repo {REPO} --body $'# Design Review\n...<!-- osc-review-design -->'
     ```
     If there are no P0/P1 design findings at all, this comment is still posted, with
     body along the lines of `# Design Review\nNo blocking design/architecture issues
     found.` plus the marker — mirroring `review-summary.md`'s "No blocking issues
     found" quiet path. **The PR-level comment must always exist after a successful
     run, independent of whether any inline comments were posted**, since it is the
     signal `has_design_review_comment` (§7.3) checks for.
  3. `DESIGN_REVIEW_MARKER` is appended to **both** comment forms above, verbatim.

  **Partial-failure duplicate-inline-comment gap (identified in review, closed here):**
  because inline findings (item 1) and the closing PR-level comment (item 2) are posted
  via separate `gh api`/`gh pr comment` calls within a single invocation, a crash or
  timeout after one or more inline comments have posted but before the PR-level comment
  is reached leaves `has_design_review_comment` (§7.3) still returning `false` on the
  next run — the *only* "already done" signal either runner currently checks. A naive
  retry would then re-run the whole prompt from scratch with no memory of the partial
  attempt, very likely regenerating and re-posting the same inline findings, duplicating
  them every retry cycle until a run finally succeeds end-to-end. This is the same
  failure shape (non-idempotent posting plus a completion signal that doesn't match the
  actual unit of work) as a prior SonarQube duplicate-comment bug in this codebase, and
  it applies symmetrically to §7.2's defense-in-depth check, which relies on the same
  marker. The fix is the "Prior `DESIGN_REVIEW_MARKER` inline comments" prompt input
  added above: because that fetch happens on every invocation — not gated by the
  PR-level noop check — a retry after a partial failure still sees its own earlier
  inline comments and skips re-flagging the same `(file, line)` pairs, even though
  `has_design_review_comment` itself hasn't flipped to `true` yet. Genuinely new findings
  at other file/line combinations, or new cross-cutting findings, are still posted
  normally, and the run still finishes by posting the closing PR-level comment.

### 7.2 Wiring into `self-review` (fate decoupled — resolved, §11.1)

- Add a design-review step to `_process_single_pr` (`harness/runners/self_review.py`),
  using the same `ctx` dict already built (`vibe_heal_context`, `pr_description`,
  `base_branch`, `commit_sha`, `files`) — no re-fetching needed.
- This step's outcome is **tracked independently** of the existing
  `if not file_failure and not summary_failure: reviewed.add(number)` gate
  (`self_review.py:263`), which is **left untouched**. A new persisted field,
  `design_reviewed_prs: list[int]`, is added to `self_review.json`
  (`harness/state.py`), read/written/pruned the same way `reviewed_prs` already is
  (including by `prune_self_review_state`, using the same `open_pr_numbers` set), but as
  a fully separate list — a PR can be in `reviewed_prs` without being in
  `design_reviewed_prs`, or vice versa, and neither list's membership check gates the
  other's retry.
- On each run: skip invoking the design backend call for a PR number already present in
  `design_reviewed_prs`. Otherwise, run it; on success (`returncode == 0`, no timeout),
  add the PR number to `design_reviewed_prs`; on failure/timeout, log it and leave the
  PR off the list so it's retried independently next run — this never touches
  `reviewed_prs`/`partial_reviews`, and a file/summary failure never blocks or resets
  this step either.
- As defense-in-depth (not the primary mechanism — the persisted list is), the step may
  also check `has_design_review_comment` before invoking, the same live-signal check
  `review-requested` relies on as its *only* mechanism (§7.3) — this guards against a
  state write that didn't persist (the same class of edge case the existing
  comment-marker check at the top of `_run_locked` already guards against for the whole
  PR). Like §7.3, this check alone cannot detect a partial failure that posted some
  inline comments without ever reaching the PR-level comment; the prior-inline-comments
  prompt input (§7.1) closes that gap here too, since it runs on every invocation
  regardless of which idempotency check gated the decision to invoke.

### 7.3 Wiring into `review-requested` (resolved, §11.2)

- Add a design-review step to `_process_pr` (`harness/runners/review_requested.py`),
  alongside `_run_file_reviews`/`_run_summary_review`.
- `review-requested` has **no persisted state**
  (`docs/commands/review-requested.md:101-116`); the design step's idempotency signal is
  therefore entirely the new marker: **before invoking**, check
  `has_design_review_comment(pr_number, repo, current_user, env)` (checking
  `repos/{repo}/issues/{pr_number}/comments` for `DESIGN_REVIEW_MARKER`, mirroring
  `check_review_summary_comment_status`'s exact pattern against the same endpoint — the
  PR-level comment from §7.1 is guaranteed to exist after any successful run, so this
  single check is sufficient without also querying the pulls/comments inline endpoint).
  If a matching comment is already present, **treat the design step as a noop this
  cycle**: do not invoke the backend again, and treat it as succeeded for the gate
  below.
- If no matching comment is found, invoke the backend once with the design prompt. This
  invocation always includes the prior-inline-comments input from §7.1, which is what
  actually protects against duplicate inline findings if an earlier invocation crashed
  mid-way through posting (see the partial-failure note in §7.1's Output contract) — the
  marker check above only protects against re-running a pass that already completed
  end-to-end.
- **`remove_reviewer` (`review_requested.py:171`) now requires all three of:**
  `files_ok and summary_ok and design_ok`, where `design_ok` is `True` either because
  the noop check above found an existing marker, or because this run's invocation
  succeeded. **If the design step fails or times out, `design_ok` is `False` and
  `remove_reviewer` is skipped this cycle** — the PR stays on
  `user-review-requested:@me` and is retried on the next run, exactly like a
  files/summary failure already causes today. This is the direct resolution of §11.2:
  a failing design pass must not cause the reviewer to be silently dropped.
- A design-pass backend timeout is handled the same way per-file and summary timeouts
  already are here: logged, non-fatal to the rest of the loop, but (per the point above)
  it does block `remove_reviewer` for that PR this cycle.

### 7.4 Anticipating the requirement-traceability pass (issue #4) — resolved, §11.5

No shared "PR-level pass" abstraction is built now. Instead, the following spots must
each carry an explicit code comment naming the future reuse opportunity, so issue #4
doesn't have to rediscover them from scratch:

- `is_design_review_comment` / `has_design_review_comment` in `common.py` — comment
  noting these are one instance of a `has_pr_level_pass_comment(marker)` shape a future
  `review-traceability.md` pass could reuse or generalize to.
- The `design_reviewed_prs`-independent-of-`reviewed_prs` pattern in
  `self_review.py`/`state.py` — comment noting this is the "decoupled fate tracking for
  a PR-level pass" shape a second PR-level pass would need too.
- The `design_ok` three-way gate added to `remove_reviewer` in `review_requested.py` —
  comment noting a second PR-level pass would extend this same gate rather than
  invent its own.

## 8. Non-functional requirements

- **Invocation cost.** One additional `Backend.run()` call per PR, per runner run, when
  the design pass hasn't already succeeded for that PR — zero additional invocations
  once it has (§7.2, §7.3), regardless of ongoing file/summary retries. Timeout reuses
  `harness.backend_timeout_seconds` — no new config field (§11.8, §4).
- **Prompt size.** Per §7.1, the prompt concatenates every changed file's diff
  PR-wide, so its input size scales with total PR diff size, not per-file size — large
  PRs could produce a prompt an order of magnitude larger than any single
  `review-file.md` invocation. No existing prompt in this codebase does this today
  (`review-summary.md` deliberately lists files without diffs), so there's no empirical
  size band to derive a limit from; this must be watched during implementation rather
  than bounded up front.
- **No new destructive operations.** Like the existing passes, this one only reads the
  checked-out working tree and posts GitHub comments; it must not commit, push, or
  request/remove reviewers itself (that stays the runner's job, as today).
- **No new configuration surface.** Unconditional wiring (§4, §11.8) means no new
  `harness.toml` field or section for this pass.

## 9. Out-of-scope / explicit exclusions

(Distinct from Non-goals in §4, which are about the effort's purpose — these are
specific behaviors considered and excluded.)

- Per-file design review (e.g. running `review-design.md` once per changed file instead
  of once per PR) — considered and rejected, since the entire motivation (§2) is that
  design flaws require cross-file visibility a per-file loop structurally cannot
  provide.
- Any narrowing of `review-file.md`'s "Maintainability" dimension — explicitly decided
  against (§11.6): this effort ships purely additive, accepting the resulting overlap
  between the two passes as a known tradeoff rather than editing an existing, tuned
  prompt file as a side effect.
- A generalized, shared PR-level-pass framework in `common.py` — explicitly deferred to
  whenever issue #4 is actually picked up (§11.5, §7.4), with reuse points marked by code
  comments in the meantime rather than built now.

## 10. System/tool shape

- **Language/runtime:** Python, matching the rest of `harness/` (this repo uses `uv`,
  not `poetry`, for running tests/tooling).
- **New/changed files, concretely:**
  - New (git-tracked, at the `dotharness` repo root — i.e. `../../knowledge/pr-review/review-design.md`
    relative to this `tools/pr-review` directory): `knowledge/pr-review/review-design.md`.
  - Changed: `harness/runners/self_review.py`, `harness/runners/review_requested.py`.
  - Changed: `harness/runners/common.py` — new `DESIGN_REVIEW_MARKER` constant, plus
    `is_design_review_comment`/`has_design_review_comment` helpers (§5, §7.3), each with
    a reuse-pointer comment per §7.4.
  - Changed: `harness/state.py` — new, independent `design_reviewed_prs` field in
    `self_review.json`'s schema (§7.2), including its own pruning in
    `prune_self_review_state`.
  - **Not changed:** `harness/config.py`, `docs/configuration.md` schema tables — no new
    config field (§8, §11.8).
  - New tests: `tests/runners/test_self_review.py`, `tests/runners/test_review_requested.py`
    additions, following those files' existing patterns (mocking `Backend.run`, `gh`
    calls via `run_cmd`); `tests/test_state.py` additions for `design_reviewed_prs`.
  - Docs: `docs/commands/self-review.md` and `docs/commands/review-requested.md` need
    their "What it does" / "State and idempotency" / "Notes" sections updated to
    describe the third pass, matching their current level of detail.
- **No new services, schedulers, or CLI subcommands** — this rides entirely inside the
  two existing `harness run self-review` / `harness run review-requested` invocations.

## 11. Decisions log (resolved 2026-09-16)

All eight items originally logged here were answered directly by the user; each
resolution is folded into the section(s) noted below, per the drafting prompt's rule
that a resolved decision must never silently disappear back into ambiguity.

1. **Fate decoupled** from the existing per-file retry cost — see G4 (§3), §7.2.
2. **`review-requested` does not remove the reviewer on design-pass
   failure/timeout, and treats the step as a noop if a marked comment from a prior
   attempt is already present** — see §7.3.
3. **Both** an inline, file-anchored comment form and one always-posted PR-level
   comment form — see §7.1 (Output contract), which also resolves the prompt-input
   question (per-file diffs are required) that this choice implied.
4. **A new, design-specific marker** (`DESIGN_REVIEW_MARKER`), not a reuse of an
   existing one — see §5, §7.1.
5. **Deferred**: no shared "PR-level pass" framework is built now; instead, every reuse
   point relevant to issue #4 gets an explicit code comment — see §4, §7.4, §9.
6. **No scope change to `review-file.md`** — it is left entirely as-is; the two passes'
   potential overlap is an accepted tradeoff, not something fixed here — see §2 (closing
   note), §4, §9.
7. **P0/P1 only**, matching `review-file.md`'s existing severity bar exactly — see §7.1
   (Severity gate).
8. **Unconditional** — no opt-in config flag or `[design_review]` section — see §4, §8.

## 12. Next step

With §11 fully resolved, carve this document into a phase file / ticket scoped to, in
this rough order: (a) writing `review-design.md`'s actual content (prompt text
implementing §7.1's scope, tone, severity gate, and output contract verbatim), (b) the
`common.py` marker/helper addition (§5, §7.3, §7.4), (c) the `self_review.py` +
`state.py` wiring (§7.2), (d) the `review_requested.py` wiring (§7.3), and (e) the
corresponding test and `docs/commands/*.md` updates — in that order, since (a)
determines the exact prompt-input shape (b)–(d) need to build, and the marker helper in
(b) is shared by both runner changes.
