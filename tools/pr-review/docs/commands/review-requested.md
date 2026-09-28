# review-requested

This command reviews open pull requests where a review has been explicitly
requested from the current `gh` user. These PRs appear under "Review requests"
for that account. For each PR, it posts review comments via the configured AI
backend in four places: per-file findings, a summary, a PR-level
design/architecture review, and a PR-level requirement-traceability review. An
optional fifth PR-level pass — the regret review — runs on top of these when
`[regret_review].enabled` is `true` (off by default): it re-checks old, ignored
review comments on the PRs this command covers (see [State and idempotency](#state-and-idempotency)).
Run it whenever you want the bot/user account to handle pending review
requests instead of scanning every open PR. Scanning every open PR is what
`review-prs` is for.

## Usage
```
harness run [--config PATH] [--verbose] review-requested [--pr PR_URL]
```

- `--config PATH` — Path to the `.harness.toml` config file. Defaults to
  `./.harness.toml` (resolved relative to the current directory).
- `--verbose` — Enables DEBUG-level logging. It also mirrors log output to
  stdout, even when stdout is not a TTY. Logs always go to
  `~/.local/share/dotharness/logs/review-requested/<date>.log`.
- `--pr PR_URL` — Optional. When given, the runner reviews only that one PR
  instead of all PRs with a pending review request. It parses the PR number as
  the last path segment of the URL. It still applies the same per-PR skip
  checks (already approved or already reviewed) and runs the same review
  pipeline as the batch case.

## What it does
1. The runner acquires an exclusive file lock. The lock key is the resolved
   `repo.working_dir` path, not `repo.name` or `repo_slug`. The other four
   commands share this lock (see [Shared behavior](index.md#shared-behavior)).
   If another instance holds it — this runner or any of the other four — the
   runner exits immediately with an error instead of blocking.
2. The runner fetches a `gh` token (via `harness.gh_token_cmd`) and builds a
   subprocess environment (`PATH` prepends + `harness.env` + `GITHUB_TOKEN`).
   It then finds the current `gh` user's login (`gh api user --jq .login`).
3. The runner builds the list of PRs to process:
    - If `--pr` is given, it resolves just that PR via `gh pr view` (number,
      url, headRefName, createdAt, closingIssuesReferences).
    - Otherwise, it runs `gh pr list --repo <repo> --state open --search user-review-requested:@me`
      with the same `--json` field list in one call.
    `createdAt` and `closingIssuesReferences` feed the traceability pass's
    ticket resolution (see below). There is no extra `gh` cost: both fields
    come from the same call. `closingIssuesReferences` are GitHub's own
    closing-keyword-derived issue links, for example from a `Fixes #N` in the
    PR body.
4. The runner loads the review prompts from
   `harness.knowledge_dir/pr-review/review-file.md`, `.../review-summary.md`,
   `.../review-design.md`, and `.../review-traceability.md`. The
   `.../review-regret.md` template is read separately, only when
   `[regret_review].enabled` is `true` and the regret pass actually runs for a
   PR (see below). If configured, it also loads the optional
   `harness.review_knowledge_file`. It constructs a `Backend` for
   `harness.backend` (`opencode` or `claude`).
5. The runner records the repo's current commit (`git checkout --detach HEAD`)
   so it can be restored after each PR.
6. For each candidate PR, in order:
    - The runner determines whether the file+summary part still has work to do
      (the "already approved / already reviewed" check below). It separately
      checks whether the design pass, the traceability pass, and — when
      `[regret_review].enabled` is `true` — the regret pass have each already
      posted their own marked comment. If all the outstanding parts are
      already done,
      the runner skips the PR entirely, without a checkout. If only some are
      outstanding, the runner still processes the PR, but runs only the
      outstanding parts.
    - The runner skips the file+summary part if the current user already left
      an `APPROVED` review on the PR. It also skips it if the current user
      already posted a comment whose body starts with `[bot]dotharness-review` or
      `Review Summary` (after stripping leading `#`/whitespace). These checks
      prevent re-reviewing the same PR revision.
    - Otherwise, the runner fetches and checks out the PR's head branch
      (using the shared rebase/reset behavior — see
      [Shared behavior](index.md#shared-behavior)). It collects any cached
      vibe-heal/SonarQube review context for the branch. It fetches the PR
      description, the base branch, and the head SHA. It diffs
      `origin/<base>...HEAD` to get the changed files.
    - For each changed file, the runner builds a prompt: the review-file
      instructions, any optional extra knowledge, the file's diff (or the
      whole file reference if the file is new or the diff covers ≥75% of it),
      PR/repo/commit metadata, the PR description, and any vibe-heal context.
      It then invokes `backend.run(prompt, cwd=<repo working dir>)`. The
      backend process (the CLI configured via `harness.backend` — see
      [Configuration](../configuration.md)) posts the inline review comments
      to GitHub. It posts them via `gh api repos/{repo}/pulls/{pr}/comments`,
      per the instructions in `review-file.md`, for any P0/P1 findings it
      identifies. A per-file backend timeout is caught and logged. The loop
      then continues to the next file.
    - After all files, the runner builds one summary prompt: the summary
      instructions, any extra knowledge, PR/repo metadata, the list of
      reviewed files, the PR description, and any vibe-heal context. It
      invokes the backend once more. Per `review-summary.md` this posts a
      single `gh pr comment` starting with `# Review Summary`. A timeout here
      is likewise caught and logged.
    - **Design pass:** Before invoking the backend, the runner checks GitHub
      directly for an existing `<!-- dotharness-review-design -->`-marked comment on
      the PR. This marker is the *only* idempotency signal for this pass,
      since this runner has no persisted state. If found, the pass is a noop:
      no backend call, and it counts as already succeeded. Otherwise, the
      runner builds one PR-wide prompt from `review-design.md` (every changed
      file's diff, concatenated — not just the files touched this run) and
      invokes the backend once. Per `review-design.md` this posts inline
      comments for file-specific P0/P1 design findings plus exactly one
      PR-level `# Design Review` comment. A timeout here is likewise caught
      and logged.
    - **Traceability pass:** Before invoking the backend, the runner checks
      GitHub directly for an existing
      `<!-- dotharness-review-traceability -->`-marked comment on the PR. This
      marker is this runner's only idempotency signal for this pass too. If
      found, the pass is a noop. Otherwise, the runner resolves the PR's
      linked ticket(s). It primarily uses `closingIssuesReferences`
      (GitHub's own closing-keyword linking) and fetches each entry with
      `gh issue view {number} --repo {owner}/{name}`. If that finds nothing,
      it uses a second mechanism: scanning issue-timeline comments posted
      within 5 minutes of the PR's `createdAt` (any author, including bots)
      for an issue reference. If neither mechanism resolves a ticket, the
      runner posts `# Requirement Traceability\nNo linked ticket
      found...` directly via `gh pr comment`, with **no backend call**. This
      outcome is terminal: it is never retried for this PR, even if a ticket
      link is added later. Otherwise, the runner builds one PR-wide prompt
      from `review-traceability.md` (the same concatenated diff as the design
      pass, plus the resolved ticket(s)' title and body, and any
      early-comment context) and invokes the backend once. Per
      `review-traceability.md` this posts inline comments for file-anchored
      P0/P1 **scope-creep** findings only (gap findings are PR-level-only,
      because they have no line to anchor to), plus exactly one PR-level
      `# Requirement Traceability` comment. A timeout here is likewise caught
      and logged.
    - **Regret pass (optional):** When `[regret_review].enabled` is `true`
      (off by default), the runner can also run the regret-review pass on
      small, bugfix-shaped PRs: it traces this PR's changed lines back to the
      commit/PR that introduced them and, when that introducing PR carried a
      review comment on the same change that the backend judges would have
      prevented this bug, posts one PR-level
      `# Previously flagged review comments` comment linking back to those
      comments (one backend invocation per PR, only when
      candidates exist; see
      [`../configuration.md`](../configuration.md#regret_review)). Before
      invoking the backend, the runner checks GitHub directly for an existing
      `<!-- dotharness-review-regret -->`-marked comment, which is this pass's *only*
      idempotency signal in this stateless runner. Unlike the backend-posted
      design and traceability comments, this one is posted by the runner
      itself: the backend replies only with verdict lines, which the runner
      parses. When the candidate search finds nothing to judge, or the backend
      judges every candidate `NO`, nothing is posted, so the marker check
      stays false and the pass simply re-runs, with a fresh candidate search,
      whenever this PR is next processed.
    - The runner removes the current user as a requested reviewer on the PR
      (`gh pr edit --remove-reviewer <login>`). This clears the PR from
      future `user-review-requested:@me` searches. It happens only if the
      file+summary part, the design pass, the traceability pass, *and* — when
      `[regret_review].enabled` is `true` — the regret pass all succeeded (or
      were already done). A failing or timed-out pass in any one of the
      passes (files, summary, design, traceability, and regret when enabled —
      a failed regret comment post counts as a failure) is enough to keep the
      PR on the reviewer's queue for a retry next run, even if the other
      parts succeeded.
    - Any other exception while processing a PR is caught and logged. The
      runner proceeds to the next PR rather than aborting the whole run.
    - Regardless of outcome, the runner restores the repo to the commit
      recorded in step 5 before moving to the next PR (or exiting).

## Configuration
Only these `.harness.toml` fields affect this runner (full schema in
[`../configuration.md`](../configuration.md)):

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode`/`claude`) runs the reviews |
| `harness.backend_timeout_seconds` | Per-invocation timeout for each backend call (per file, for the summary, and for the design/traceability passes when they have not already succeeded) |
| `harness.gh_token_cmd` | Command used to fetch the `GITHUB_TOKEN` passed to `gh` and the backend |
| `harness.knowledge_dir` | Where `pr-review/review-file.md`, `pr-review/review-summary.md`, `pr-review/review-design.md`, `pr-review/review-traceability.md`, and — only when `[regret_review].enabled` is `true` — `pr-review/review-regret.md` prompt templates live |
| `harness.path_prepend` | Extra `PATH` entries for subprocesses (git/gh/backend) |
| `harness.env` | Extra environment variables merged into the subprocess/backend env |
| `harness.review_knowledge_file` | Optional extra instructions appended to the file, summary, design, and traceability prompts, and — only when `[regret_review].enabled` is `true` — to the regret-judgment prompt |
| `repo.name` | GitHub repo slug used for all `gh` calls |
| `repo.working_dir` | Local git checkout where the runner detaches, fetches, and checks out branches. Its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |
| `repo.subdir[].path` | Used to locate each subdir's `sonar-project.properties` project key, to find cached vibe-heal review output for the PR branch |

This runner does not read `[vibe_heal]` fields directly. It only consumes
pre-existing vibe-heal review output on disk
(`~/.vibe-heal/reviews/<project_key>/<branch>/review.md`), if any exists for
the branch. It finds that output via `repo.subdirs`.

This runner reads the `[regret_review]` fields only when
`regret_review.enabled` is `true` (off by default); when the pass is off, no
other field of that section is consulted and the pass costs nothing. See
[`../configuration.md`](../configuration.md#regret_review) for the full
schema.

## State and idempotency
This runner does not use the `state.py` module. There is no persisted
"last processed PR" or "last SHA reviewed" record. Instead, it avoids
duplicate work using live signals read from GitHub on every run:
1. It skips the file+summary part if the current user already has an
   `APPROVED` review on the PR.
2. It skips the file+summary part if the current user already posted a
   comment starting with `[bot]dotharness-review` or `Review Summary`.
3. It skips the design pass, independently of (1) and (2), if the current
   user already posted a comment containing `<!-- dotharness-review-design -->`.
   This marker is the design pass's *only* idempotency signal. Its fate is
   deliberately decoupled from (1) and (2), so a retry of one does not force
   a retry of the other.
4. It skips the traceability pass, independently of (1)-(3), if the current
   user already posted a comment containing
   `<!-- dotharness-review-traceability -->`. This marker covers both a completed
   scope/gap comparison *and* the terminal "no linked ticket found" outcome,
   since both post that same marker. Like the design pass, this marker is the
   traceability pass's *only* idempotency signal in this runner, and its
   fate is decoupled from (1)-(3).
5. When `[regret_review].enabled` is `true` (off by default), it skips the
   regret pass, independently of (1)-(4), if the current user already posted
   a comment containing `<!-- dotharness-review-regret -->`. This marker is the
   regret pass's *only* idempotency signal in this stateless runner, and its
   fate is decoupled from (1)-(4). Unlike the design and traceability
   markers, it is posted only when the backend confirms at least one regret
   finding: when the candidate search finds nothing to judge, or the backend
   judges every candidate `NO`, nothing is posted, so the marker stays absent
   and the pass simply re-runs, with a fresh candidate search, whenever this
   PR is next processed.
6. After reviewing, it removes itself as a requested reviewer. It does this
   only once the file+summary part, the design pass, the traceability pass,
   and — when `[regret_review].enabled` is `true` — the regret pass have all
   succeeded (or were already done). This removes the PR from the
   `user-review-requested:@me` search used to build the batch list next run.

Because of (6), re-requesting review from the bot/user account is what
triggers reprocessing. Pushing new commits also triggers it, since GitHub
re-requests review automatically depending on branch protection settings.
There is no SHA comparison. So if the reviewer is manually re-requested
without new commits, and no matching comment or approval exists, the PR will
be reviewed again.

## Notes
- This runner uses the shared locking and working-directory-mutation
  behavior described in
  [Commands → Shared behavior](index.md#shared-behavior). It additionally
  runs its git commands with `--recurse-submodules`, so submodule state moves
  with the branch.
- Failure isolation: a PR that raises an exception (for example, a failed
  `gh` or git call) is logged and skipped. It does not stop the rest of the
  batch. It is not marked "done" in any way, so the runner retries it on the
  next run.
- Backend timeouts are non-fatal at the per-file, summary, design, and
  traceability steps. They are logged, and the run proceeds to the next
  step. A slow or hung backend on one file does not block review of the rest.
  Reviewer removal requires `files_ok and summary_ok and design_ok and
  traceability_ok` — and, when `[regret_review].enabled` is `true`,
  `regret_ok` as well. So a timeout at any of those keeps the PR on the queue
  for a retry next run. There is no special-casing of the design or
  traceability pass relative to the other two. See
  [State and idempotency](#state-and-idempotency).
- Building the batch list makes one `gh pr list --search` call. No extra
  per-PR call is needed: `headRefName`, `createdAt`, and
  `closingIssuesReferences` all come from that one call.
- The traceability pass's extra `gh` cost: one `gh issue view` call per
  resolved ticket (typically one). It also makes one paginated fetch of the
  PR's issue-timeline comments for the early-comment-window fallback scan,
  but only when the closing-keyword mechanism finds nothing. Both are cheap,
  non-LLM `gh` calls in the same class as the other passes' marker checks.
- The regret pass's cost is likewise independent, and gated. It runs only
  when `[regret_review].enabled` is `true` (off by default), and at most one
  backend invocation per PR — zero when the candidate search (blame →
  introducing-PR lookup → comment match) finds nothing to judge, which posts
  nothing. Unlike `self-review`, this runner persists no state for this
  pass, so a run that posts nothing (no candidates, or every candidate
  judged `NO`) simply re-runs the bounded candidate search the next time the
  PR is processed. Its `git blame`/`gh api`/comment-fetch cost is bounded by
  `[regret_review].max_diff_lines` before any blame work, and the whole
  per-PR sequence of `git`/`gh` calls is additionally capped by
  `[regret_review].regret_review_timeout`.
