# Prompt: Draft a Requirements Document

> This prompt was originally extracted from an early draft of what is now
> `spec-prism-flow/docs/requirements.md`. That document remains a live example of the output
> this prompt produces. Its renumbering since extraction means the original section locator no
> longer maps cleanly, so it is omitted here rather than guessed. Paste this whole file into a
> coding session. Then add a rough brief: idea, existing code, conventions doc, and reference
> links. This drives drafting of a project's `requirements.md`. This doc is one level up from
> phase files or user stories. It is the single source of truth that those later, smaller work
> orders will quote from — not the work order itself.

## Your task

Turn a rough, possibly ambiguous brief into a requirements document. Make it precise enough that
a later process can split it into small, independently implementable work orders (phases,
tickets, user stories). That process should not need to re-derive intent, re-ask settled
questions, or guess at anything the brief left open. You are not writing prose for a human to
admire. You are writing the document a downstream agent (or engineer) will quote from
verbatim. Every ambiguity you cannot resolve from the brief becomes an explicit, answerable
question — never a silent assumption.

## Required structure

Use these sections, in this order. Omit a section only if it is genuinely inapplicable. If you
omit one, say so explicitly instead of leaving a gap. The closer this doc matches this structure,
the more usable everything downstream is.

1. **Purpose / origin.** State why this document exists and what it is based on. A source may be
   a prior pattern being generalized, a reference implementation, a competitor, or a rough idea
   with no reference at all. State this plainly so a reader knows how much of what follows is
   derived or invented.

2. **Problem statement.** State what is broken or missing today, in concrete terms. State why
   solving it needs a deliberate process rather than a haphazard effort. Ground it in an observed
   failure mode or gap, not an abstract aspiration.

3. **Goals.** Write a numbered list (`G1`, `G2`, …). Make each goal a single, falsifiable
   outcome, not a direction. "Support X for any Y" beats "make things better." IDs let later
   documents (phase files, tickets) cite a goal instead of re-explaining it.

4. **Non-goals.** State what this effort deliberately will *not* do, even though it is adjacent
   or tempting. This is where you stop scope creep before it starts. If you considered an idea
   and rejected it, say so and say why. A rejected idea with no rationale leads someone to
   revisit it needlessly.

5. **Glossary** (if the domain has any term that could be misread). Define each term once, here,
   and never redefine it downstream. A term that needs a definition twice is ambiguous.

6. **Feature/component breakdown.** If the effort has more than one component, add a short
   at-a-glance table (question it answers, shape, input, output) before the detailed sections.
   This orients a reader before they reach the detail.

7. **Detailed functional requirements, section by section.** This is the most important part of
   the document. For each functional area:
   - State the behavior specifically enough to quote **verbatim** into a smaller downstream work
     order. A later reader should never need to open this document again once they have the
     excerpt.
   - Name concrete things precisely: exact file, hook, and command names; exact CLI subcommands;
     and exact data shapes. Never say "follow conventions" or "handle appropriately."
   - If part of this section belongs to a different downstream work order, say so explicitly.
     Set that boundary the way you would define a phase's scope.
   - Surface known ambiguities, discrepancies, or "don't fix this" traps inline. Add an explicit
     instruction not to resolve them unilaterally.

8. **Non-functional requirements.** Cover performance, sizing bands, safety limits, and
   operational constraints. These are things downstream work must satisfy but that are not
   features in themselves. When you have data to derive bands from, prefer empirically derived
   bands (with their observed range) over invented targets.

9. **Out-of-scope / explicit exclusions** (if distinct from Non-goals — Non-goals is about the
   effort's purpose, while this is about the specific features and behaviors you considered and
   excluded).

10. **System/tool shape** (if applicable). State where this lives, how it is built, and what it
    is made of. Be concrete enough that scaffolding can start immediately. Do not say "some kind
    of CLI."

11. **Open decisions log.** Write a numbered list of every question you could not resolve from
    the brief alone. Phrase each as a concrete, answerable question — not "TBD" or a vague
    concern. Add enough context that a human can answer it without re-reading the whole
    document. An unresolved, flagged question is a valid, honest terminal state for this
    section. Track it here rather than letting it disappear.

12. **Next step.** State what happens immediately after this document is approved. Be concrete.
    Do not say "then we build it."

## Properties every version of this document must have

- **Verbatim-quotable.** A downstream work order should be able to copy-paste anything it needs
  from here, rather than refer to it and hope. Write for that copy-paste, not for narrative flow.
- **ID'd and traceable.** Number the goals, phases, and decisions. Later documents can then cite
  an ID instead of re-describing intent. A reviewer can check that every numbered item was
  addressed somewhere.
- **Explicit non-goals, always.** A document with goals but no non-goals has not actually bounded
  scope yet.
- **Ambiguity surfaced, never guessed.** If the brief does not say something, do not infer it
  silently. Write the question down in the open-decisions log. A wrong guess costs far more than
  an explicit question.
- **Testable, not aspirational.** Prefer requirements you can turn into a checklist item directly
  ("returns X given Y"). Avoid ones that only a human can judge subjectively ("works well").
- **Sized to reality, not invented.** When you have empirical data (an existing corpus, prior
  examples), derive the sizing bands or structural rules from it and cite the source range. Do
  not invent round numbers.
- **Self-contained per section.** A reader should be able to jump straight to the section they
  need without reading the ones before it. Cross-reference by number rather than assuming
  context.

## Process guidance

- Treat this as iterative, not one-shot. Draft from the brief. Flag every gap in the
  open-decisions log. Expect a human checkpoint before the document is "approved." Approval means
  the log has been reviewed and answered, or explicitly deferred — not that drafting stopped.
- Never let a resolved answer disappear back into ambiguity. When a human answers an open
  decision, fold the answer into the relevant section. Then either remove the log entry or mark
  it resolved with a pointer to where it landed.
- Before declaring the document done, self-check it against the "Properties" list above and the
  "Required structure" list. Call out explicitly any section you omitted and why.


## Approved overview

# regret-review — Overview

## 1. Problem statement

When a bug is fixed, the code being corrected was often reviewed once already. Sometimes a
reviewer flagged the exact issue back then, in a comment that was never addressed. That "regret"
is invisible today. Nothing connects a bugfix PR back to the review comment on the original PR
that introduced the bug. So the same kind of oversight (from the same author, or a different one)
tends to recur unnoticed.

`regret-review` is a new pass inside `tools/pr-review` (`harness/runners/`). For small
bugfix-shaped PRs, it traces each changed line back to the PR that introduced it. It checks
whether that original PR carried a review comment on those lines. If an AI backend judges that
comment would have prevented the bug now being fixed, the pass posts a comment on the current PR
linking back to it. The goal is to surface a pattern ("this kind of comment keeps getting
ignored") that is otherwise buried in review history no one re-reads.

## 2. Goals

- For a small bugfix PR, detect whether any changed line traces back (via `git
  blame`) to a commit whose originating PR received a review comment on that same line or
  region.
- Use the AI backend to judge whether addressing that comment back then would have prevented the
  bug being fixed now. Give it the current PR's diff (the fix) and the original PR's review
  comment. Filter out comments that are unrelated, stylistic, or already addressed.
- Post exactly one comment on the current PR per genuine "regret" finding. Link it back to the
  original review comment (its PR, and ideally the specific comment or URL). Gate it with a
  completion marker so a rerun does not repost.
- Gate the whole pass behind a `[regret_review]` block in `.harness.toml`. Mirror the existing
  `[vibe_heal]` block's shape: an `enabled` flag plus its own settings, off by default.
- Only run against PRs that look bugfix-shaped. Use a configurable max-changed-lines threshold
  (the issue's "≤N lines, configurable"). This keeps the pass cheap and its signal precise. Large
  PRs would blame far more lines than a human could usefully read regret findings about.

## 3. Non-goals

- Not a general "find stale review comments" auditor. It only looks at comments on lines that a
  *current, small, bugfix-shaped* PR is touching. It never proactively scans old PRs on its own.
- Not responsible for judging whether the *current* PR's fix is itself correct. That is the job
  of design review or self review. `regret-review` only judges the connection between an old
  comment and the new fix.
- Not extending to non-GitHub-native history. If the introducing commit cannot be mapped to a PR
  (for example, it was pushed directly to the base branch, or the repo or PR was deleted), the
  pass skips that line rather than guessing.
- Not building a shared git-blame, diff, or log-parsing library. Per dotharness#21's decisions
  log, that extraction is deferred until a third consumer actually needs it. `regret-review`
  implements its own blame and PR-lookup plumbing directly in this runner, alongside vibe-heal's
  and code-health's separate copies.
- Not a subprocess or JSON integration with an external tool, the way `[vibe_heal]` is. Issue #33
  only asks `regret-review` to *mirror that block's config style*. It lives in-process in
  `harness/runners/`, like `self_review` or `review_requested`, not as a wrapper around a
  separately installed CLI.

## 4. Glossary

- **Current PR** — the PR under review right now, the one a `regret-review` run is evaluating.
  Its diff is "the bug now being fixed."
- **Introducing commit** — the commit that `git blame` attributes a changed line's *prior*
  content to. That is, it blames the base-branch version of the lines the current PR's diff
  replaces or removes, not the new lines the fix adds.
- **Introducing PR** — the pull request that merged the introducing commit into the base branch.
  The pass resolves it via GitHub's "list pull requests associated with a commit" API, not by
  parsing merge-commit messages.
- **Regret finding** — a case where the introducing PR carried a review comment on the blamed
  line or region, and the backend judges that addressing it back then would have prevented the
  bug the current PR fixes.
- **Regret comment** — the comment that `regret-review` posts on the current PR. It links back to
  the original (ignored) review comment and is marked with its own completion marker. This
  mirrors `INLINE_REVIEW_MARKER` or `DESIGN_REVIEW_MARKER` in `harness/runners/common.py`.
- **Bugfix-shaped PR** — a PR whose total changed-line count is at or below a configurable
  threshold. This is this pass's proxy for "small, focused fix," per issue #33.

## 5. Key decisions already implied by the brief

- **Blame targets the pre-fix content, not the fix itself.** "For each changed line" in issue #33
  must mean the lines the current PR's diff *removes or modifies*. Blame those against the base
  branch. That is what finds the commit (and PR) that introduced the bug. Blaming the PR's newly
  *added* lines would only point back at the current PR's own commit, which is meaningless.
- **Commit → PR resolution uses GitHub's own API**, not commit-message parsing.
  `GET /repos/{owner}/{repo}/commits/{sha}/pulls` (exposed via `gh api
  repos/{owner}/{repo}/commits/{sha}/pulls`) returns the PR that carried a given commit. This
  avoids unreliable merge-commit-message scraping and works regardless of squash, merge, or
  rebase strategy. So it is treated as settled rather than left open.
- **Original-PR comments are fetched with the existing `fetch_pr_comments` shape**
  (`harness/runners/common.py`, which itself shells out to `scripts/pr-comments.py fetch
  --pr N`). It accepts an arbitrary PR number via `gh pr view N`. It is not restricted to the
  current branch's PR. So no new fetch mechanism is needed. The pass only makes a call pointed at
  the introducing PR's number instead of the current one.
- **Config shape mirrors `VibehealConfig`** (`harness/config.py`). Use a `RegretReviewConfig`
  dataclass with `enabled: bool = False` plus its own settings. Parse it from a `[regret_review]`
  `.harness.toml` table, the same way `[vibe_heal]` is parsed. See `OPEN_QUESTIONS.md` for
  exactly which settings.
- **Orchestration follows the existing PR-level-pass skeleton** (`run_pr_level_pass` in
  `harness/runners/common.py`): skip if already posted → build prompt → invoke the backend →
  verify the completion marker landed. This is the same skeleton `self_review.py`'s design and
  traceability passes already use, not a new orchestration pattern.
- **Hooks into `self_review` and `review_requested`, not `review_prs`.** Scope it to PRs a human
  is actually paying attention to right now. That means the current user's own open PRs
  (`self_review`) and PRs where review was explicitly requested from the harness account
  (`review_requested`). This is not a full batch sweep of every open PR in the repo. This mirrors
  where the existing design-review and traceability-review passes already run (both runners). So
  `regret_review` joins them as a third pass in the same two places, rather than opening a new
  integration point via `review_prs`.
- **Line matching uses a diff-hunk tolerance window, not exact file and line.** A review comment
  on the introducing PR counts as a match if its (path, line) falls within the same contiguous
  diff hunk (unified-diff `@@` region) that the introducing commit's change touched. It does not
  have to be on the exact blamed line. This absorbs line drift from force-pushes, later commits
  within the same PR, or minor renumbering. It still requires the same file and the same
  localized region of change, not a whole-file or whole-PR match.
- **`[regret_review]` config shape**, mirroring `VibehealConfig`'s style: `enabled: bool = False`,
  `max_diff_lines: int = 50` (the bugfix-shaped threshold, measured the same way
  `build_file_review_section` already counts diff lines: added and removed lines, summed across
  every changed file in the PR), `authors: str | list[str]
  = "*"` (mirrors `VibehealConfig.authors`), and `regret_review_timeout: int = 300` (a per-PR
  wall-clock budget for this pass's git and `gh` calls plus the backend invocation, since the
  blame step, the commit-to-PR lookup, and the comment fetch can each mean several sequential
  API calls per changed line).
- **A line whose introducing commit or PR cannot be resolved (deleted PR, direct push to the base
  branch, or private or renamed repo) is silently skipped**. It is not logged as an error or
  surfaced to the user. This is consistent with the fail-open convention every existing
  `common.py` helper already follows for inconclusive lookups (for example,
  `list_open_prs_matching_authors`, `resolve_linked_tickets`).
