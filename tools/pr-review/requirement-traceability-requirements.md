# Requirements: Requirement-Traceability Review Pass (diff vs. linked ticket)

> Drafted per `~/.harness/tools/requirements-doc-drafting-prompt.md`, from
> [dotharness#4](https://github.com/alexeieleusis/dotharness/issues/4) (referenced as
> "#future-2" in [dotharness#3](https://github.com/alexeieleusis/dotharness/issues/3),
> which this document's sibling, `design-review-requirements.md`, explicitly deferred
> generalizing anything for — see §4 there). This is the single source of truth a later,
> smaller work order (a phase file / ticket) will quote from — not the work order itself.
> Four architectural forks, plus the two items originally logged open in §11, were raised
> directly to the user during drafting and answered (2026-09-17); all six answers are
> folded into §7 below and are not re-litigated. §11 records the resolutions with
> pointers rather than leaving them to silently disappear.

## 1. Purpose / origin

`dotharness` already runs three automated PR-level/per-file passes: `review-file.md`
(per-file correctness), `review-summary.md` (one-shot PR summary), and `review-design.md`
(PR-level design/architecture, added by dotharness#3 — see `design-review-requirements.md`).
None of them check whether a PR's diff actually matches what was *asked for*. Issue #4
proposes a fourth pass whose only job is that comparison — catching scope creep (diff
does more than the ticket asked) and gaps (ticket asks for X, diff doesn't cover it) —
and issue #3 explicitly required the two issues be designed jointly so neither multiplies
`self-review`'s per-file retry-on-failure cost (currently ~2(N+1) invocations on a failed
run, per PR #39's description).

The originating issue's "Proposed shape" asked for a generic, configurable tracker
convention ("Linear/Jira/GitHub Issues, whichever the team uses"). During this drafting
session the user narrowed and concretized that into a specific mechanism: resolve the
linked ticket from the PR itself (GitHub's own closing-keyword linking, with an
early-comment scan as fallback), synthesize a "formal requirement" from it, and compare
that against the PR's full diff — flagging scope creep and gaps. That refinement is what
this document specifies. It is not derived from a reference implementation for the
comparison logic itself, but it *is* derived directly from `review-design.md`'s existing,
shipped shape (prompt template location/tone, dual inline+PR-level output contract, the
`DESIGN_REVIEW_MARKER`/decoupled-fate pattern) — this pass reuses that shape almost
exactly, differing mainly in what it reads (a resolved ticket, not just the diff) and
what it flags (scope creep / gaps, not abstraction/placement).

Four design forks were resolved directly by the user (2026-09-17), and are stated here
so later sections don't need to re-justify them:

1. **Ticket resolution:** GitHub's native closing-keyword linking
   (`closingIssuesReferences`, derived from `Fixes #N`/`Closes #N`/etc. in the PR body)
   is the **primary** signal. Scanning early comments for issue links is a **fallback**,
   only attempted when closing-keyword linking finds nothing. (Original comment-only idea
   is demoted to fallback — see §7.2.)
2. **Comparison input:** the pass compares the resolved ticket against the **full PR
   diff** (the same per-file diff concatenation `review-design.md` already receives),
   not against the `review-summary.md` comment. The Review Summary is a deliberately
   terse, lossy 3-6 sentence summary explicitly told not to repeat individual findings
   (`knowledge/pr-review/review-summary.md:8-13`) — comparing against it risked hiding
   exactly the scope creep/gaps this pass exists to catch. This also means the
   traceability pass has **no ordering dependency** on `review-summary.md` completing
   first; it is fully independent, like `review-design.md`.
3. **No linked ticket found:** post a PR-level comment saying so, rather than silently
   skipping — keeps the "exactly one PR-level comment marks a pass as done" idempotency
   convention uniform across all PR-level passes.
4. **Early-comment scan includes bot-authored comments** — the link itself is often
   posted by an integration bot (a Jira/GitHub bridge, etc.), not a human, so excluding
   bots would defeat the fallback's purpose.

## 2. Problem statement

Today, nothing automated checks whether a PR actually does what its ticket asked. A human
reviewer either re-reads the ticket themselves (easy to skip under time pressure) or
trusts the PR description's own framing of its scope. Two failure modes follow:

- **Scope creep is invisible until a human notices it.** A PR can quietly do more than
  its ticket authorized — an unrelated refactor bundled in, a second feature slipped into
  a bugfix PR — and none of the three existing passes are positioned to catch it:
  `review-file.md` reviews one file in isolation (no notion of "the ticket"),
  `review-design.md` looks at abstraction/placement across files but never at intent, and
  `review-summary.md` only describes what the diff does, never checks it against what was
  asked.
- **Gaps are equally invisible.** A ticket can ask for three things and the PR can
  deliver two, and nothing today flags the missing third — the PR still "looks done" from
  a correctness/design standpoint.

Without a dedicated pass, the only place either failure mode gets caught is a human
re-reading the ticket during code review — after the PR is already built, the most
expensive point to discover a mismatch.

## 3. Goals

- **G1.** Every PR run through `self-review` or `review-requested` gets exactly one
  additional, PR-level (not per-file) automated pass whose sole focus is comparing the
  PR's diff against its resolved linked ticket — never correctness, design, or
  performance findings already owned by the other three passes. It runs unconditionally,
  the same way `review-design.md` does — no opt-in config flag (§7.6, §9).
- **G2.** The new pass's prompt template (`review-traceability.md`) is a standalone file,
  structurally and conventionally identical to `review-design.md`/`review-summary.md`,
  so it's authored/edited/versioned the same way (§7.1).
- **G3.** The pass integrates into both `self-review` and `review-requested` without
  changing either runner's existing idempotency guarantees for the other three passes —
  re-running against an already-fully-reviewed PR must not re-post duplicate traceability
  comments (§7.4, §7.5).
- **G4.** The pass's completion is tracked **independently** from every other pass's
  retry state (`reviewed_prs`/`partial_reviews`/`design_reviewed_prs`), for the same
  reason G4 in `design-review-requirements.md` gives: a traceability-pass failure never
  forces re-running the other three passes, and vice versa (§7.4, §7.5).
- **G5.** The linked ticket is resolved without requiring any new tracker integration
  (Linear/Jira) in this iteration — GitHub's own PR-to-issue linking plus an
  issue-comment fallback is sufficient, since dotharness already operates entirely
  against GitHub via `gh` (§4, §7.2).
- **G6.** Findings are traceable to the specific file(s) they concern when they can be
  (scope-creep findings anchor to the file introducing unauthorized scope); gap findings,
  which by definition point at something *absent* from the diff, are PR-level only, since
  there is no line to anchor an absence to (§7.1, §7.3).

## 4. Non-goals

- **Not adding Linear/Jira (or any other tracker) integration.** GitHub Issues only, this
  iteration — resolved directly per the user's refined brief (§1). The originating
  issue's "configurable convention" language is satisfied entirely by GitHub's own
  closing-keyword linking plus the comment-scan fallback; no tracker-specific API client
  is introduced.
- **Not wiring into `review-prs`.** Same rationale as `design-review-requirements.md` §4:
  `review-prs` is a structurally different pipeline (`vibe_heal`) with no existing
  per-file/summary pass to sit alongside.
- **Not touching `review-file.md`, `review-summary.md`, or `review-design.md`'s existing
  scope, output contract, or markers.** This pass is additive only, exactly like
  `review-design.md` was additive to `review-file.md`.
- **Not building a generic "PR-level pass" *state-tracking* framework.** Per
  `design-review-requirements.md` §9, that generalization was explicitly deferred "to
  whenever issue #4 is actually picked up" — this is that moment, but only for the
  narrow, already-flagged reuse point named there: the marker-comment-detection helper
  (§7.6). The independent-per-pass state-list pattern (`design_reviewed_prs`,
  `traceability_reviewed_prs` as two separate fields) and the runner-side `_ok`-gate
  pattern stay duplicated exactly as `review-design.md` left them — extending those
  further is still out of scope here.
- **Not re-triggering any pass on new commits pushed after it already completed.** This
  is a pre-existing property shared by all four passes (file/summary/design/traceability
  alike) — not something this effort changes or is responsible for fixing.
- **Not adding a config flag or `[traceability_review]` section.** Runs unconditionally,
  same as `review-design.md` (§7.6, §9).

## 5. Glossary

- **PR-level pass**, **Backend**, **invocation**, **Knowledge dir**, **Marker** — defined
  identically to `design-review-requirements.md` §5; not redefined here.
- **Linked ticket** — a GitHub Issue resolved as "what this PR was asked to do," via
  either of two mechanisms (§7.2): **native linking** (GitHub's own closing-keyword
  parsing, exposed as `closingIssuesReferences`) or, only as a **fallback** when native
  linking finds nothing, an issue reference found by scanning comments posted in the
  **early-comment window** (§7.2) after the PR opened.
- **Early-comment window** — the fixed period starting at the PR's `createdAt` timestamp
  and lasting `TRACEABILITY_COMMENT_WINDOW_SECONDS` (300, i.e. 5 minutes — §7.2, §11.2).
  Because it's anchored to PR creation time rather than "now," it's the same fixed window
  no matter when the pass actually runs; a fallback comment scan doesn't need to run
  within 5 minutes of PR creation to work correctly.
- **Scope creep finding** — a P0/P1 judgment that some part of the diff does something
  the linked ticket did not ask for.
- **Gap finding** — a P0/P1 judgment that the linked ticket asked for something the diff
  does not (fully) deliver.
- **`TRACEABILITY_REVIEW_MARKER`** — the new marker this effort introduces:
  `"<!-- dotharness-review-traceability -->"`, appended to every comment this pass posts,
  whether inline or PR-level, mirroring `DESIGN_REVIEW_MARKER`
  (`harness/runners/common.py:23`).

## 6. Feature/component breakdown

| Component | Question it answers | Shape | Input | Output |
|---|---|---|---|---|
| `review-traceability.md` prompt template | What counts as scope creep vs. a gap, and how should findings be posted? | Markdown instructions file, git-tracked at `knowledge/pr-review/review-traceability.md` | Resolved linked ticket(s) title+body, early-comment context, PR description, full per-file diff concatenation, PR metadata | Always exactly one PR-level `gh pr comment` (findings, "no issues found", or "no linked ticket found"), plus zero or more inline comments for file-anchored scope-creep findings — all carrying `TRACEABILITY_REVIEW_MARKER` |
| Ticket-resolution helper | Which GitHub issue(s), if any, is this PR answering? | New `resolve_linked_tickets()` in `harness/runners/common.py` (§7.2) | The `pr` dict (extended to carry `closingIssuesReferences`/`createdAt`), repo, env | List of `{number, repo, title, body, source}` dicts, possibly empty |
| Early-comment context helper | What extra clarifying context (if any) did people add right after opening the PR? | New `build_early_comment_context()` in `common.py` (§7.2) | PR number, repo, PR `createdAt`, env | Concatenated text block of early issue-timeline comments (possibly empty string) |
| Generalized PR-level-pass marker helper | Reusable "did this bot already post its PR-level completion comment" check | `has_pr_level_pass_comment(marker, ...)` extracted in `common.py`, with `has_design_review_comment`/`has_traceability_review_comment` becoming thin wrappers (§7.6) | Marker string, PR number, repo, current_user, env | Boolean (or `None` for inconclusive), same tri-state contract as today |
| `self_review.py` / `review_requested.py` wiring | When does each runner invoke the pass, and how does its outcome affect state / `remove_reviewer`? | New, independent step in each runner, mirroring the `review-design.md` wiring exactly | Same `ctx` dict already built per PR | Success/failure tracked in `self_review.json`'s new `traceability_reviewed_prs` field (self-review); gates `remove_reviewer` via an added `traceability_ok` term (review-requested) |

## 7. Detailed functional requirements

### 7.1 The `review-traceability.md` prompt template

- **Location:** `knowledge/pr-review/review-traceability.md` — new, git-tracked, added
  the same way `review-design.md` was.
- **Scope of judgment**, restated from §1/§3: given a resolved linked ticket's
  description and the PR's full diff, judge only two things:
  1. **Scope creep** — does any part of the diff do something not asked for by the
     ticket? (Not a design/abstraction judgment — that's `review-design.md`'s job. A
     scope-creep finding is about *authorization*, not code quality: "this change wasn't
     part of what was asked," independent of whether the change itself is well-written.)
  2. **Gaps** — does the diff fail to (fully) deliver something the ticket explicitly
     asked for?
  Explicitly **not**: correctness bugs, design/abstraction, performance, or style — those
  stay with the other three passes, unchanged.
- **Tone/Perspective/Role** follow the same preamble pattern as `review-design.md`
  (`knowledge/pr-review/review-design.md:6-33`) so all four passes read as one coherent
  reviewer persona.
- **Severity gate: P0/P1 only**, same two-tier bar as the other passes. For this pass:
  P0 = the diff implements something substantively absent from or contradicting the
  ticket (major scope creep), or omits a capability the ticket explicitly and centrally
  asked for (major gap). P1 = a partial or moderate deviation in either direction. This
  exact line is drafted into `review-traceability.md` itself, not left implicit.
- **Required inputs to the prompt** (built by the runner):
  - PR URL, PR number, repo name, head commit SHA, PR description (`get_pr_description`)
    — identical to `build_design_review_prompt`'s existing inputs
    (`harness/runners/common.py:611-648`).
  - **Per-file diff content, concatenated across every changed file** — the same
    `build_file_review_section` output per file that `review-design.md` already receives
    (§1 decision 2), reusing the same per-PR diff cache both runners already build for
    the design pass (the `ctx["files"]`/`_cached_file_diff` machinery — see the existing
    code comment "Gathered once and shared by all three passes below" in
    `review_requested.py:189-192`, which becomes "all four passes" after this change).
  - **The resolved linked ticket(s)**: each entry's title + body from
    `resolve_linked_tickets()` (§7.2), labeled with its source (native link vs.
    comment-scan fallback) so the model can weight native-linked tickets as more
    authoritative if more than one ticket is found.
  - **Early-comment context** from `build_early_comment_context()` (§7.2) — this runs
    and is included *regardless* of which mechanism resolved the ticket, since a
    clarifying comment posted right after opening (e.g. "scope reduced to just X") is
    useful context even when the ticket itself was found via closing-keyword linking.
  - **Prior `TRACEABILITY_REVIEW_MARKER` inline comments already posted on this PR** —
    same partial-failure protection `review-design.md` has (§7.1's "Already-flagged"
    section in `design-review-requirements.md`), via a new
    `get_traceability_review_flagged_locations()` mirroring
    `get_design_review_flagged_locations` (`harness/runners/common.py:344-367`) exactly,
    fetched on every invocation (not gated by the noop check).
  - Optional `harness.review_knowledge_file` extra-guidance and vibe-heal context,
    exactly as the other three passes already append.
- **If no linked ticket was resolved at all** (§7.2 finds nothing via either mechanism):
  the prompt is never built and the backend is never invoked for this step — the runner
  posts the "no linked ticket found" PR-level comment directly (§7.3), without a backend
  call. (This differs from `review-design.md`, which always invokes the backend — here,
  "no ticket" is a purely mechanical, deterministic-from-GitHub-API outcome with nothing
  for a model to judge.)
- **Output contract**, mirroring `review-design.md`'s dual form
  (`design-review-requirements.md` §7.1, items 1-3) with one difference:
  1. **Inline findings** (P0/P1 **scope-creep** findings only, file-anchored): posted via
     `gh api repos/{REPO}/pulls/{PR_NUMBER}/comments` exactly as `review-design.md` does,
     with `TRACEABILITY_REVIEW_MARKER` appended.
     **Gap findings are never posted inline** — a gap is an absence, and has no file/line
     to anchor to; gaps only ever appear in the PR-level comment.
  2. **One PR-level comment, always posted exactly once per successful run**, via
     `gh pr comment {PR_NUMBER} --repo {REPO} --body $'# Requirement Traceability\n...'`:
     - If a ticket was resolved: link back to it (`Linked ticket: #N` or the cross-repo
       `owner/repo#N` form), briefly summarize any scope-creep/gap findings under
       `## Scope creep` / `## Gaps` headings (only the headings with findings are
       included), or state `No scope creep or requirement gaps found relative to the
       linked ticket.` if there are none.
     - This comment must always exist after a successful run, independent of whether any
       inline comments were posted — it is the signal `has_traceability_review_comment`
       checks for (§7.4/§7.5).
  3. `TRACEABILITY_REVIEW_MARKER` is appended to every comment form above, verbatim.

### 7.2 Resolving the linked ticket

New functions in `harness/runners/common.py`:

- **`resolve_linked_tickets(pr: dict, repo: str, env: dict) -> list[dict]`**
  1. **Native linking (primary):** read `pr.get("closingIssuesReferences", [])`. Each
     entry (verified live against this repo's own PR #39, which closes issue #3) has the
     shape `{"number": int, "url": str, "repository": {"name": str, "owner": {"login":
     str}}}` — no `title`/`body`. For each entry, fetch title+body via
     `gh issue view {number} --repo {owner}/{name} --json number,title,body`, using the
     entry's own `repository.owner.login`/`repository.name` (not necessarily the PR's own
     repo — a closing reference can point at a different repository). Tag each resolved
     ticket `source: "closing_keyword"`.
  2. **Comment-scan fallback:** only attempted if step 1 yields an empty list. Fetch
     early-window comments via the same fetch `build_early_comment_context` uses (§7.2
     below shares one underlying fetch), and scan each comment body (regardless of
     author, including bots — §1 decision 4) for issue references matching: a full URL
     (`https://github.com/{owner}/{repo}/issues/{N}`), a same-repo shorthand (`#N`), or a
     cross-repo shorthand (`{owner}/{repo}#N`). For each match, resolve via `gh issue
     view {N} --repo {owner}/{repo}` (using the PR's own repo when the match didn't
     specify one); a match that 404s, or that resolves to a pull request rather than an
     issue (GitHub shares one number sequence between issues and PRs per repo), is
     dropped rather than treated as a ticket. Successfully resolved tickets are tagged
     `source: "comment"`, deduplicated by `(repo, number)`.
  2. Requires `pr` to carry `closingIssuesReferences` (and, for §7.3's window
     computation, `createdAt`) — **both runners' PR-listing `--json` field lists must be
     extended** from `number,url,headRefName` to
     `number,url,headRefName,createdAt,closingIssuesReferences`
     (`harness/runners/review_requested.py:111`, `harness/runners/self_review.py:456`).
     Both fields are supported by `gh pr list --json` (verified via `gh pr list --help`),
     so this adds zero extra API calls.

- **`build_early_comment_context(pr_number: int, repo: str, pr_created_at: str, env:
  dict) -> str`**
  Fetches ALL issue-timeline comments (`repos/{repo}/issues/{pr_number}/comments`, raw
  REST, paginated) — **not** via `fetch_pr_comments`/`scripts/pr-comments.py`, whose
  cached `issue_comments` entries drop the `created_at` timestamp entirely
  (`scripts/pr-comments.py:125-135`), and **not** via `common.py`'s existing
  `_fetch_matching_comments`/`_has_matching_comment`, which filter to `current_user`'s
  own comments only (`harness/runners/common.py:260-296`) — exactly backwards from what's
  needed here, since the ticket link is posted by *someone else*. A new, separate
  paginated fetch (mirroring `address_comments.py`'s `_fetch_all_pages` pattern,
  `harness/runners/address_comments.py:396-420`) is required, returning raw comment dicts
  with `body` and `created_at` (REST's snake_case field — distinct from `pr["createdAt"]`,
  which comes from `gh pr view`/`gh pr list --json` and is camelCase). Filters to comments
  where `created_at <= pr_created_at + TRACEABILITY_COMMENT_WINDOW_SECONDS` (string-compare
  on ISO-8601 timestamps is safe here the same way `address_comments.py:441` already
  relies on it), concatenates their bodies (including bot-authored ones) into one text
  block. Returns `""` if there are none, or if the fetch itself fails (fails open — a
  missing early-comment fetch degrades the pass's context, not its correctness).

- **`TRACEABILITY_COMMENT_WINDOW_SECONDS = 300`** — a new constant in `common.py`, not a
  config field this iteration (resolved, §11.2): 300 seconds (5 minutes) is confirmed as
  the right window, hardcoded now with no `harness.toml` surface; a future config field
  can be added later without changing this design if real usage shows it needs tuning.

### 7.3 No linked ticket found

If `resolve_linked_tickets` returns an empty list (both mechanisms found nothing):

```
gh pr comment {PR_NUMBER} --repo {REPO} --body $'# Requirement Traceability\nNo linked ticket found (no closing-keyword link and no issue reference in the first 5 minutes of comments) — skipping scope/gap comparison.\n<!-- dotharness-review-traceability -->'
```

posted directly by the runner (no backend invocation — see §7.1). **This outcome is
terminal (resolved, §11.1)**: once posted, `has_traceability_review_comment` finds this
comment on every later check and the pass is never retried for this PR, even if the PR
body or a later comment goes on to add a proper ticket link. This pass is a courtesy
check, not the PR author's or reviewer's obligation enforced by the harness — a PR that
never gets a ticket linked simply carries a permanent "not applicable" outcome rather than
being retried indefinitely, with no special-cased marker body or re-resolution logic
needed: the existing "any `TRACEABILITY_REVIEW_MARKER` comment means done" check already
does the right thing unmodified.

### 7.4 Wiring into `self-review`

Mirrors `design-review-requirements.md` §7.2 exactly, substituting traceability for
design:

- Add a traceability-review step to `_process_single_pr`
  (`harness/runners/self_review.py`), using the same `ctx` dict already gathered.
- New, independent persisted field `traceability_reviewed_prs: list[int]` in
  `self_review.json` (`harness/state.py`), read/written/pruned the same way
  `design_reviewed_prs` already is (`harness/state.py:138-214`) — including its own entry
  in `prune_self_review_state`. A PR's membership in `traceability_reviewed_prs` is
  independent of `reviewed_prs` and `design_reviewed_prs`; no list's membership gates
  another's retry.
- Skip invoking the backend for a PR number already in `traceability_reviewed_prs`.
  Otherwise: resolve the ticket (§7.2); if none found, post the §7.3 comment directly and
  mark done — terminal, per §7.3/§11.1; if found, build and run the traceability prompt; on
  success (backend exit 0 **and** a confirmed `TRACEABILITY_REVIEW_MARKER` PR-level
  comment — mirroring `_run_design_review`'s post-hoc GitHub check,
  `harness/runners/self_review.py:213-260`, itself added to close the missing-summary bug
  class this codebase has hit before), add the PR to `traceability_reviewed_prs`; on
  failure/timeout, log and leave it off the list for retry next run.
- Defense-in-depth: also check `has_traceability_review_comment` before invoking, mirror
  of `design-review-requirements.md` §7.2's equivalent check.

### 7.5 Wiring into `review-requested`

Mirrors `design-review-requirements.md` §7.3 exactly:

- Add a traceability-review step to `_process_pr`
  (`harness/runners/review_requested.py`), alongside the existing design-review step
  (`review_requested.py:205-207`).
- No persisted state exists in this runner; idempotency is entirely
  `has_traceability_review_comment` (checking `repos/{repo}/issues/{pr_number}/comments`
  for `TRACEABILITY_REVIEW_MARKER`), exactly mirroring `has_design_review_comment`'s role
  here.
- **`remove_reviewer` now requires all four of:** `files_ok and summary_ok and design_ok
  and traceability_ok` — this is the literal reuse point the existing code comment at
  `review_requested.py:200-204` names ("A future PR-level pass (issue #4) would extend
  this same AND-gate below with its own `_ok` boolean rather than invent a parallel
  gating mechanism"). If the traceability step fails or times out,
  `traceability_ok` is `False` and `remove_reviewer` is skipped this cycle, exactly like
  a design-pass failure already does.

### 7.6 Extracting the shared marker-check helper (closing the design-review reuse pointer)

`design-review-requirements.md` §4/§9 explicitly deferred generalizing the "PR-level pass
already posted its completion comment" check "to whenever issue #4 is actually picked
up," and the shipped code already carries the literal reuse-pointer comment for it
(`harness/runners/common.py:330-332`, on `has_design_review_comment`: *"A future PR-level
pass (e.g. requirement-traceability, dotharness#4) could reuse this same shape as
`has_pr_level_pass_comment(marker)` rather than duplicating it."*). This is that moment,
scoped narrowly to exactly what that comment names — **not** a broader "PR-level pass
framework":

- Add `has_pr_level_pass_comment(marker: str, pr_number: int, repo: str, current_user:
  str, env: dict) -> bool` and its tri-state `check_pr_level_pass_comment_status(marker,
  ...) -> bool | None` counterpart to `common.py`, built on the existing
  `_has_matching_comment`/`_fetch_matching_comments` machinery
  (`harness/runners/common.py:260-296`) with `is_design_review_comment`/
  `is_traceability_review_comment`-style predicates parameterized by `marker` instead of
  hardcoded.
- `has_design_review_comment`/`check_design_review_comment_status` become thin wrappers
  calling the generalized functions with `DESIGN_REVIEW_MARKER` — **no behavior change**,
  existing tests must still pass unmodified (or with only mechanical updates for the
  refactor, not new assertions).
- `has_traceability_review_comment`/`check_traceability_review_comment_status` are built
  the same way, from day one, with `TRACEABILITY_REVIEW_MARKER`.
- The per-pass **state-list** duplication (`design_reviewed_prs`,
  `traceability_reviewed_prs` as two separate top-level fields) and the runner-side
  `_ok`-gate duplication (§7.5) are explicitly **not** touched by this extraction — see
  §4. Only the stateless marker-check helper is generalized.

## 8. Non-functional requirements

- **Invocation cost.** At most one additional `Backend.run()` call per PR per runner run
  (zero when a ticket can't be resolved at all — §7.1/§7.3 short-circuit before any
  backend call in that case), and zero once the pass has already succeeded for that PR,
  regardless of ongoing file/summary/design retries — identical cost profile to
  `review-design.md` (`design-review-requirements.md` §8).
- **Extra GitHub API cost.** One `gh issue view` call per resolved ticket (typically one),
  plus one paginated comment fetch for the early-comment window (§7.2) — all cheap,
  non-LLM `gh` calls, same class of cost as the other passes' existing marker-check calls.
  No extra cost for `closingIssuesReferences`/`createdAt` themselves, since both are
  folded into the existing PR-listing call (§7.2).
- **Prompt size.** Same band as `review-design.md`
  (`design-review-requirements.md` §8): the full per-file diff concatenation dominates
  prompt size, plus a typically-small addition for the ticket body and early-comment
  text. No new size limit is introduced; watch during implementation, same as the design
  pass.
- **No new destructive operations.** Reads the working tree and GitHub API, posts
  comments — never commits, pushes, or requests/removes reviewers itself.
- **No new configuration surface.** Unconditional wiring, no new `harness.toml` field —
  `TRACEABILITY_COMMENT_WINDOW_SECONDS` is a code constant, not a config value (§7.2,
  §11.2).

## 9. Out-of-scope / explicit exclusions

- Any tracker other than GitHub Issues (Linear, Jira) — see §4, G5.
- Any narrowing of `review-file.md`/`review-summary.md`/`review-design.md`'s existing
  scope — this pass is purely additive.
- Generalizing the per-pass state-list or runner `_ok`-gate patterns beyond the one named
  reuse point (§7.6) — considered, and deliberately left duplicated a second time, same
  as `design-review-requirements.md` §9 left it duplicated a first time.
- Treating a bare `#N` mention *inside the PR body* (as opposed to a recognized closing
  keyword) as a ticket link — GitHub's own `closingIssuesReferences` only reflects
  recognized closing keywords; a non-closing-keyword body mention (e.g. "Related: #42")
  is not picked up by either mechanism in this design and is explicitly excluded, not
  silently handled.
- Fetching or considering the linked issue's *own* comments (as opposed to its
  title/body) as part of the requirement context — out of scope, keeps the ticket context
  the same shape the originating issue described ("the ticket's description").

## 10. System/tool shape

- **Language/runtime:** Python, `uv`-managed, matching the rest of `harness/`.
- **New/changed files:**
  - New: `knowledge/pr-review/review-traceability.md`.
  - Changed: `harness/runners/common.py` — `TRACEABILITY_REVIEW_MARKER` constant,
    `TRACEABILITY_COMMENT_WINDOW_SECONDS` constant, `resolve_linked_tickets`,
    `build_early_comment_context`, a new paginated-all-authors comment fetch,
    `get_traceability_review_flagged_locations`, `build_traceability_review_prompt`,
    `has_pr_level_pass_comment`/`check_pr_level_pass_comment_status` (§7.6) plus
    `is_traceability_review_comment`/`has_traceability_review_comment`/
    `check_traceability_review_comment_status`, and refactoring the design-review
    equivalents into thin wrappers over the generalized helper.
  - Changed: `harness/runners/self_review.py`, `harness/runners/review_requested.py` —
    new wiring per §7.4/§7.5; both runners' PR-listing `--json` field lists extended per
    §7.2.
  - Changed: `harness/state.py` — new, independent `traceability_reviewed_prs` field,
    including its own pruning in `prune_self_review_state`.
  - **Not changed:** `harness/config.py`, `docs/configuration.md` schema tables — no new
    config field.
  - New/changed tests: `tests/runners/test_self_review.py`,
    `tests/runners/test_review_requested.py`, `tests/runners/test_common_comments.py` /
    a new `tests/runners/test_common_traceability.py`, `tests/test_state.py` additions;
    plus regression coverage for the §7.6 refactor of the design-review marker helpers.
  - Docs: `docs/commands/self-review.md` and `docs/commands/review-requested.md` updated
    to describe the fourth pass, matching their current level of detail.
- **No new services, schedulers, or CLI subcommands** — rides entirely inside the
  existing `harness run self-review` / `harness run review-requested` invocations.

## 11. Decisions log (resolved 2026-09-17)

Both items originally logged here were answered directly by the user; each resolution is
folded into the section(s) noted below, per the drafting prompt's rule that a resolved
decision must never silently disappear back into ambiguity.

1. **Terminal, not re-checked.** The "no linked ticket found" PR-level comment (§7.3) is
   a permanent outcome, same as every other pass's completion marker — this pass is a
   courtesy check, not an obligation the harness enforces, so there's no expectation it
   should keep retrying a PR whose author never linked a ticket. No distinguishable
   marker body or re-resolution logic is needed; the existing "any
   `TRACEABILITY_REVIEW_MARKER` comment means done" check is already correct as
   designed — see §7.3, §7.4.
2. **300 seconds confirmed, stays a hardcoded constant.** The 5-minute early-comment
   window is correct as proposed and remains `TRACEABILITY_COMMENT_WINDOW_SECONDS = 300`
   in `common.py`, not a `harness.toml` field in this iteration — a config surface can be
   added later without changing this design if real usage calls for tuning it — see §7.2,
   §8.

## 12. Next step

With §11 fully resolved, carve this document into a phase file / ticket scoped to, in
this order: (a) the §7.6 marker-helper extraction and regression tests for the existing
design-review wrappers (lowest risk, unblocks nothing else but should land first since
(b)-(e) build on the generalized helper existing), (b)
`resolve_linked_tickets`/`build_early_comment_context`/the new paginated comment fetch in
`common.py` (§7.2), (c) writing `review-traceability.md`'s actual prompt content (§7.1),
(d) the `self_review.py` + `state.py` wiring (§7.4), (e) the `review_requested.py` wiring
(§7.5) — in that order, since (a)/(b) are what (c)-(e) build on.

**Progress (branch `4_requirement-traceability`):**
- [x] (a) done — commit `365d380` (`common.py`: `has_pr_level_pass_comment` /
  `check_pr_level_pass_comment_status` extracted, `has_design_review_comment` /
  `check_design_review_comment_status` are now thin wrappers with no behavior change,
  `TRACEABILITY_REVIEW_MARKER` + `is_/has_/check_traceability_review_comment*` added;
  19 new tests in `tests/runners/test_common_prs.py`; full suite 444 passed, ruff +
  `ty check` clean). Requirements doc itself landed in commit `6e584d3`.
- [ ] (b) `resolve_linked_tickets` / `build_early_comment_context` / the new
  paginated-all-authors comment fetch in `common.py` (§7.2) — not started.
- [ ] (c) `knowledge/pr-review/review-traceability.md` prompt content (§7.1) — not
  started.
- [ ] (d) `self_review.py` + `state.py` wiring (§7.4) — not started.
- [ ] (e) `review_requested.py` wiring (§7.5) — not started.
- [ ] Docs updates to `docs/commands/self-review.md` / `docs/commands/review-requested.md`
  (§10) — not started; do this alongside (d)/(e), not as an afterthought.
