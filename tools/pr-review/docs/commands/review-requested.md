# review-requested

Reviews open pull requests where GitHub review has been explicitly requested from
the current `gh` user (i.e. PRs that show up under "Review requests" for that
account), posting per-file and summary review comments via the configured AI
backend. Run it whenever you want the bot/user account to act on pending review
requests instead of scanning every open PR (that's what `review-prs` is for).

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
1. Acquires an exclusive file lock keyed on `repo_slug`, shared with the other four commands
   (see [State and idempotency](#state-and-idempotency)); if another instance — this
   command or any of the other four — holds it, the command exits immediately with an
   error instead of blocking.
2. Fetches a `gh` token (via `harness.gh_token_cmd`) and builds a subprocess
   environment (`PATH` prepends + `harness.env` + `GITHUB_TOKEN`). Looks up
   the current `gh` user's login (`gh api user --jq .login`).
3. Builds the list of PRs to process:
   - If `--pr` was given, resolves just that PR via `gh pr view` (number,
     url, headRefName).
   - Otherwise, runs `gh search prs user-review-requested:@me --repo <repo> --state open`
     to get candidate PR numbers/URLs, then hydrates each with its
     `headRefName` via `gh pr view` (search doesn't return that field
     directly). PRs without a resolvable `headRefName` are dropped.
4. Loads the review prompts from `harness.knowledge_dir/pr-review/review-file.md`
   and `.../review-summary.md`, plus the optional
   `harness.review_knowledge_file` if configured, and constructs a `Backend`
   for `harness.backend` (`opencode` or `claude`).
5. Records the repo's current commit (`git checkout --detach HEAD`) so it can
   be restored after each PR.
6. For each candidate PR, in order:
   - Skips it if the current user already left an `APPROVED` review on it, or
     if the current user already posted a comment whose body starts with
     `osc-review` or `Review Summary` (after stripping leading `#`/whitespace)
     — this is the mechanism that prevents re-reviewing the same PR revision.
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
   - Removes the current user as a requested reviewer on the PR
     (`gh pr edit --remove-reviewer <login>`), which is what clears it from
     future `user-review-requested:@me` searches.
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
| `harness.backend_timeout_seconds` | Per-invocation timeout for each backend call (per file, and for the summary) |
| `harness.gh_token_cmd` | Command used to fetch the `GITHUB_TOKEN` passed to `gh` and the backend |
| `harness.knowledge_dir` | Where `pr-review/review-file.md` and `pr-review/review-summary.md` prompt templates live |
| `harness.path_prepend` | Extra `PATH` entries for subprocesses (git/gh/backend) |
| `harness.env` | Extra environment variables merged into the subprocess/backend env |
| `harness.review_knowledge_file` | Optional extra instructions appended to both the file and summary prompts |
| `repo.name` | GitHub repo slug used for all `gh` calls, and the lock key (`name` with `/` replaced by `-`) |
| `repo.working_dir` | Local git checkout the runner detaches, fetches, and checks branches out in |
| `repo.subdir[].path` | Used to locate each subdir's `sonar-project.properties` project key, to find cached vibe-heal review output for the PR branch |

`[vibe_heal]` fields are not read by this runner directly — it only consumes
pre-existing vibe-heal review output on disk (`~/.vibe-heal/reviews/<project_key>/<branch>/review.md`),
if any exists for the branch, via `repo.subdirs`.

## State and idempotency
This runner does not use the `state.py` module — there is no persisted
"last processed PR" or "last SHA reviewed" record. Instead it avoids
duplicate work using two live signals read from GitHub on every run:
1. It skips a PR if the current user already has an `APPROVED` review on it.
2. It skips a PR if the current user already posted a comment starting with
   `osc-review` or `Review Summary`.
3. After reviewing, it removes itself as a requested reviewer, which is what
   drops the PR out of the `user-review-requested:@me` search used to build
   the batch list next run.

Because of (3), re-requesting review from the bot/user account (or pushing
new commits, which GitHub re-requests review for automatically depending on
branch protection settings) is what triggers reprocessing — there's no SHA
comparison, so if the reviewer is manually re-requested without new commits
and no matching comment/approval exists, the PR will be reviewed again.

## Notes
- Uses the shared locking and working-directory-mutation behavior described in
  [Commands → Shared behavior](index.md#shared-behavior); this runner additionally
  runs its git commands with `--recurse-submodules`, so submodule state moves with
  the branch.
- Failure isolation: a PR that raises an exception (e.g. a failed `gh` or git
  call) is logged and skipped — it does not stop the rest of the batch, and it
  does not get marked "done" in any way, so it will be retried on the next
  run.
- Backend timeouts are non-fatal at both the per-file and summary steps: they're
  logged and the run proceeds (to the next file, or to
  removing the reviewer), so a slow/hung backend on one file doesn't block
  review of the rest.
- Building the batch list makes one `gh search prs` call plus one `gh pr view` call per
  candidate PR (to hydrate `headRefName`), so a repo with many pending review
  requests means proportionally many `gh` invocations.
