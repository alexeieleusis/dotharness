# review-requested

Reviews open pull requests where GitHub review has been explicitly requested from
the current `gh` user (i.e. PRs that show up under "Review requests" for that
account), posting per-file, summary, PR-level design/architecture, and PR-level
requirement-traceability review comments via the configured AI backend. Run it
whenever you want the bot/user account to act on pending review requests instead
of scanning every open PR (that's what `review-prs` is for).

## Usage
```
harness run [--config PATH] [--verbose] review-requested [--pr PR_URL]
```

- `--config PATH` — path to the `.harness.toml` config file. Defaults to
  `./.harness.toml` (resolved relative to the current directory).
- `--verbose` — enables DEBUG-level logging, and also mirrors log output to
  stdout even when stdout isn't a TTY. Logs always go to
  `~/.local/share/dotharness/logs/review-requested/<date>.log`.
- `--pr PR_URL` — optional. When given, the command reviews only that one PR
  (the PR number is parsed as the last path segment of the URL) instead of
  scanning for all PRs with a pending review request. It still runs through
  the same per-PR skip checks (already approved / already reviewed) and the
  same review pipeline as the batch case.

## What it does
1. Acquires an exclusive file lock keyed on the resolved `repo.working_dir` path (not
   `repo.name`/`repo_slug` — see [Shared behavior](index.md#shared-behavior)), shared with
   the other four commands; if another instance — this command or any of the other four —
   holds it, the command exits immediately with an error instead of blocking.
2. Fetches a `gh` token (via `harness.gh_token_cmd`) and builds a subprocess
   environment (`PATH` prepends + `harness.env` + `GITHUB_TOKEN`). Looks up
   the current `gh` user's login (`gh api user --jq .login`).
3. Builds the list of PRs to process:
   - If `--pr` was given, resolves just that PR via `gh pr view` (number, url,
     headRefName, createdAt, closingIssuesReferences).
   - Otherwise, runs `gh pr list --repo <repo> --state open --search user-review-requested:@me`
     with the same `--json` field list in one call.
   `createdAt` and `closingIssuesReferences` (GitHub's own closing-keyword-derived
   issue links, e.g. from a `Fixes #N` in the PR body) feed the traceability pass's
   ticket resolution (see below) at no extra `gh` cost, since both fields come back
   from this same call.
4. Loads the review prompts from `harness.knowledge_dir/pr-review/review-file.md`,
   `.../review-summary.md`, `.../review-design.md`, and `.../review-traceability.md`,
   plus the optional `harness.review_knowledge_file` if configured, and constructs a
   `Backend` for `harness.backend` (`opencode` or `claude`).
5. Records the repo's current commit (`git checkout --detach HEAD`) so it can
   be restored after each PR.
6. For each candidate PR, in order:
   - Determines whether the file+summary pipeline still has work to do (the
     "already approved / already reviewed" check below), and separately
     whether the design pass and the traceability pass have each already
     posted their own marked comment. If all three are already done, the PR
     is skipped entirely with no checkout. If only some are outstanding, the
     PR is still processed, but only those parts run.
   - The file+summary part is skipped if the current user already left an
     `APPROVED` review on the PR, or if the current user already posted a
     comment whose body starts with `[bot]osc-review` or `Review Summary`
     (after stripping leading `#`/whitespace) — this is the mechanism that
     prevents re-reviewing the same PR revision.
   - Otherwise: fetches and checks out the PR's head branch (using the shared
     rebase/reset behavior — see [Shared behavior](index.md#shared-behavior)),
     collects any cached vibe-heal/SonarQube review context for the branch,
     fetches the PR description, base branch, and head SHA, and diffs
     `origin/<base>...HEAD` to get the changed files.
   - For each changed file, builds a prompt (review-file instructions +
     optional extra knowledge + the file's diff, or the whole file reference
     if it's new or the diff covers ≥75% of it + PR/repo/commit metadata + PR
     description + any vibe-heal context) and invokes
     `backend.run(prompt, cwd=<repo working dir>)`. The backend process
     (the CLI configured via `harness.backend` — see [Configuration](../configuration.md))
     is the one that actually posts inline review comments to GitHub — via
     `gh api repos/{repo}/pulls/{pr}/comments` per the instructions in
     `review-file.md` — for any P0/P1 findings it identifies. A per-file
     backend timeout is caught and logged; the loop continues to the next
     file.
   - After all files, builds one summary prompt (summary instructions + extra
     knowledge + PR/repo metadata + list of reviewed files + description +
     vibe-heal context) and invokes the backend once more; per
     `review-summary.md` this posts a single `gh pr comment` starting with
      `# Review Summary`. A timeout here is likewise caught and logged.
   - **Design pass:** before invoking, checks GitHub directly for an existing
     `<!-- osc-review-design -->`-marked comment on the PR (the *only*
     idempotency signal for this pass, since this runner has no persisted
     state) — if found, this is a noop: no backend call, and the pass counts
     as already succeeded. Otherwise, builds one PR-wide prompt from
     `review-design.md` (every changed file's diff, concatenated — not just
     the files touched this run) and invokes the backend once; per
     `review-design.md` this posts inline comments for file-specific P0/P1
     design findings plus exactly one PR-level `# Design Review` comment. A
     timeout here is likewise caught and logged.
   - **Traceability pass:** before invoking, checks GitHub directly for an
     existing `<!-- osc-review-traceability -->`-marked comment on the
     PR (this runner's only idempotency signal for this pass too) — if found,
     it's a noop. Otherwise, resolves the PR's linked ticket(s): primarily via
     `closingIssuesReferences` (GitHub's own closing-keyword linking), each
     entry fetched with `gh issue view {number} --repo {owner}/{name}`;
     falling back, only if that finds nothing, to scanning issue-timeline
     comments posted within 5 minutes of the PR's `createdAt` (any author,
     including bots) for an issue reference. If neither mechanism resolves a
     ticket, the runner posts `# Requirement Traceability\nNo linked ticket
     found...` directly via `gh pr comment` — **no backend call** — and this
     outcome is terminal (never retried for this PR, even if a ticket link is
     added later). Otherwise it builds one PR-wide prompt from
     `review-traceability.md` (the same concatenated diff as the design pass,
     plus the resolved ticket(s)' title/body and any early-comment context)
     and invokes the backend once; per `review-traceability.md` this posts
     inline comments for file-anchored P0/P1 **scope-creep** findings only
     (gap findings, having no line to anchor to, are PR-level-only) plus
     exactly one PR-level `# Requirement Traceability` comment. A timeout here
     is likewise caught and logged.
   - Never removes the current user as a requested reviewer, even once every
     pass has succeeded — this runner only posts automated findings; the human
     still reviews and approves the PR themselves (which is what actually
     clears the review request on GitHub). If any pass submits a formal review
     as a side effect, or races a concurrent runner that does (e.g.
     `review-prs`'s vibe_heal post), the PR would otherwise silently vanish
     from `user-review-requested:@me` before that submission was intentional —
     so the whole per-PR block re-adds the reviewer afterward if they held
     that status going in, the same `preserve_reviewer_request` guard
     `review-prs`, `focused-review`, and `address-comments` use (see
     [Shared behavior](index.md#shared-behavior)).
   - Any other exception while processing a PR is caught and logged; the
     command moves on to the next PR rather than aborting the whole run.
   - Regardless of outcome, restores the repo to the commit recorded in step 5
     before moving to the next PR (or exiting).

## Configuration
Only these `.harness.toml` fields affect this runner (full schema in
[`../configuration.md`](../configuration.md)):

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode`/`claude`) runs the reviews |
| `harness.backend_timeout_seconds` | Per-invocation timeout for each backend call (per file, for the summary, and for the design/traceability passes when they haven't already succeeded) |
| `harness.gh_token_cmd` | Command used to fetch the `GITHUB_TOKEN` passed to `gh` and the backend |
| `harness.knowledge_dir` | Where `pr-review/review-file.md`, `pr-review/review-summary.md`, `pr-review/review-design.md`, and `pr-review/review-traceability.md` prompt templates live |
| `harness.path_prepend` | Extra `PATH` entries for subprocesses (git/gh/backend) |
| `harness.env` | Extra environment variables merged into the subprocess/backend env |
| `harness.review_knowledge_file` | Optional extra instructions appended to the file, summary, design, and traceability prompts |
| `repo.name` | GitHub repo slug used for all `gh` calls |
| `repo.working_dir` | Local git checkout the runner detaches, fetches, and checks branches out in; its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |
| `repo.subdir[].path` | Used to locate each subdir's `sonar-project.properties` project key, to find cached vibe-heal review output for the PR branch |

`[vibe_heal]` fields are not read by this runner directly — it only consumes
pre-existing vibe-heal review output on disk (`~/.vibe-heal/reviews/<project_key>/<branch>/review.md`),
if any exists for the branch, via `repo.subdirs`.

## State and idempotency
This runner does not use the `state.py` module — there is no persisted
"last processed PR" or "last SHA reviewed" record. Instead it avoids
duplicate work using live signals read from GitHub on every run:
1. It skips the file+summary part of the pipeline if the current user already
   has an `APPROVED` review on the PR.
2. It skips the file+summary part if the current user already posted a comment
   starting with `[bot]osc-review` or `Review Summary`.
3. It skips the design-review part, independently of (1) and (2), if the
   current user already posted a comment containing `<!-- osc-review-design -->`
   — this is the design pass's *only* idempotency signal, and its fate is
   deliberately decoupled from (1)/(2) so neither part's retry forces the
   other's.
4. It skips the traceability-review part, independently of (1)-(3), if the
   current user already posted a comment containing
   `<!-- osc-review-traceability -->` — this covers both a completed
   scope/gap comparison *and* the terminal "no linked ticket found" outcome,
   since both post that same marker. Like the design pass, this is the
   traceability pass's *only* idempotency signal in this runner, and its
   fate is decoupled from (1)-(3).
5. It never removes itself as a requested reviewer, even once every part above
   has succeeded — the human still reviews and approves the PR themselves,
   and that (or GitHub's own bookkeeping around a submitted review) is what
   actually clears the request. If a pass submits a formal review as a side
   effect, or races a concurrent runner that does (e.g. `review-prs`'s
   vibe_heal post), the whole per-PR block re-adds the reviewer afterward if
   they held that status going in — the same `preserve_reviewer_request`
   guard `review-prs`, `focused-review`, and `address-comments` use.

Because the PR only drops out of the `user-review-requested:@me` search once
the human submits their own review, this runner keeps reprocessing a PR every
run until (1)-(4) are all satisfied — there's no SHA comparison, so pushing
new commits (which GitHub re-requests review for automatically depending on
branch protection settings) or manually re-requesting review resets nothing
extra; it's (1)-(4) reading live GitHub state on every run that keeps this
idempotent either way.

## Notes
- Uses the shared locking and working-directory-mutation behavior described in
  [Commands → Shared behavior](index.md#shared-behavior); this runner additionally
  runs its git commands with `--recurse-submodules`, so submodule state moves with
  the branch.
- Failure isolation: a PR that raises an exception (e.g. a failed `gh` or git
  call) is logged and skipped — it does not stop the rest of the batch, and it
  does not get marked "done" in any way, so it will be retried on the next
  run.
- Backend timeouts are non-fatal at the per-file, summary, design, and
  traceability steps: they're logged and the run proceeds to the next step, so
  a slow/hung backend on one file doesn't block review of the rest. Since a
  marker/approval check (not an in-memory success flag) is each part's only
  idempotency signal, a timeout at any of the four simply leaves that part's
  marker unposted, so it's retried next run — there's no special-casing of the
  design or traceability pass relative to the other two (see
  [State and idempotency](#state-and-idempotency)).
- Building the batch list makes one `gh pr list --search` call (no per-PR
  hydration needed — `headRefName`, `createdAt`, and `closingIssuesReferences`
  all come back from that one call).
- The traceability pass's extra `gh` cost: one `gh issue view` call per
  resolved ticket (typically one), plus — only when the closing-keyword
  mechanism finds nothing — one paginated fetch of the PR's issue-timeline
  comments for the early-comment-window fallback scan. Both are cheap,
  non-LLM `gh` calls in the same class as the other passes' marker checks.
