# self-review

`self-review` finds your own open pull requests on GitHub and has the configured AI backend review them, file by file, posting inline feedback and a summary — the same way a colleague would review your PR before you ask a human to look at it. Unlike `review-prs` (which reviews other people's PRs) or `review-requested` (which reacts to explicit review requests), this command filters strictly by PR author: it only ever looks at PRs opened by the currently authenticated `gh` user (`--author @me`). Run it after pushing a PR, or on a schedule, to get an automated first pass before requesting human review.

## Usage
```
harness run [--config PATH] [--verbose] self-review
```

- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml` (resolved relative to the current directory). This option belongs to the `run` group, so it must come before `self-review` on the command line.
- `--verbose` — enables DEBUG-level logging and (in addition to the per-day log file) also streams logs to stdout even when not attached to a TTY. Logs are written to `~/.local/share/dotharness/logs/self-review/<date>.log` regardless of this flag.

## What it does

1. Acquires a per-repo file lock (`repo_slug`), shared with the other four commands, so two invocations against the same repo — `self-review` or any of the other four — can't run concurrently; a second run exits immediately with an error instead of racing the first.
2. Resolves a GitHub token by running `harness.gh_token_cmd` (default `gh auth token`) and builds a subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
3. Loads the set of PR numbers already recorded as reviewed from state, then lists the caller's own open PRs via `gh pr list --repo <repo> --author @me --state open`, sorted by PR number.
4. Loads the shared prompt templates `review-file.md` and `review-summary.md` from `harness.knowledge_dir/pr-review/`, plus the optional `harness.review_knowledge_file` (appended to every prompt as an "Additional Review Guide" section).
5. Constructs the `Backend` (opencode or claude, per `harness.backend`) and records the repo's current `HEAD` as a detached commit, so the working tree can always be restored.
6. For each PR not already in the reviewed set:
   - If a prior comment on the PR already starts with an `osc-review` or `Review Summary` marker (i.e. it's already been reviewed, possibly by a previous run whose state write didn't happen), the PR is marked reviewed in state and skipped — no new review is generated.
   - Otherwise it fetches and checks out the PR's head branch (using the shared rebase/reset behavior — see [Shared behavior](index.md#shared-behavior)), then computes the diff against the PR's base branch.
   - For each changed file, it builds a review prompt (file diff, or the whole file if it's new or the diff touches ≥75% of it) plus PR metadata, the PR description, and any matching vibe-heal static-analysis context, and runs the backend against it. The backend is responsible for producing/posting the actual inline PR review comments — this command supplies the prompt and repo checkout, not the GitHub API calls.
   - After all files, it builds one more prompt from `review-summary.md` (listing every file reviewed) and runs the backend once more to produce the overall PR summary/comment.
   - The working tree is always restored to the recorded detached `HEAD` afterward (even on failure), before the next PR is processed.
   - The PR is only added to the reviewed set in state — and only then persisted to disk — if every backend invocation for that PR (all files plus the summary) exited 0 without timing out. If any invocation fails or times out, the PR is left unmarked so the *entire* file list is retried from scratch on the next run.
   - Errors while processing a single PR (e.g. a git checkout failure) are logged and that PR is skipped; the loop continues with the remaining PRs.

## Configuration

Only these `.harness.toml` fields affect `self-review`; see [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode` or `claude`) runs the review and summary prompts |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation (one per changed file, plus one for the summary) |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/review-file.md` and `pr-review/review-summary.md` prompt templates |
| `harness.review_knowledge_file` | Optional extra guidance appended to every prompt, if the path exists |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for both git subprocesses and the backend |
| `repo.name` | The GitHub repo (`owner/name`) queried via `gh` |
| `repo.working_dir` | Local git checkout used to fetch/checkout PR branches and diff files |
| `repo.subdir[].path` | Only used to locate each subdir's `sonar-project.properties`, so a matching vibe-heal `review.md` (if one exists on disk) can be included as static-analysis context |

`[vibe_heal]` settings are **not** read by `self-review` itself — that section controls a separate analysis step elsewhere in the tool. `self-review` only opportunistically picks up whatever vibe-heal review file already exists on disk for the PR's branch and matching Sonar project key. `repo.opencode_dir` is also not used by this command.

## State and idempotency

State is stored at `~/.local/share/dotharness/state/<repo_slug>/self_review.json` and tracks:
- `version` — schema version (currently `1`)
- `reviewed_prs` — list of PR numbers already reviewed (or already carrying an `osc-review`/`Review Summary` comment)

PRs in `reviewed_prs` are skipped on subsequent runs, so re-running `self-review` is safe and only does work for PRs opened (or newly qualifying) since the last successful pass. To force everything to be re-reviewed, clear the state:
```
harness state reset self-review --config PATH [--yes]
```
This deletes `self_review.json` for the repo after an interactive confirmation (skipped with `--yes`).

## Notes

- A PR is marked reviewed only if *every* file's review and the final summary all succeeded in the same run; a single timed-out or failing file means the whole PR — including files that succeeded — gets re-sent to the backend next time. There's no per-file progress tracking within a PR.
- The "already reviewed" check is comment-based, not state-based: it looks for any PR comment whose body starts with `osc-review` or `Review Summary` (after stripping leading `#`/spaces). This means a manually posted comment with one of those exact prefixes will cause `self-review` to consider the PR already reviewed and skip it.
- Subject to the shared `gh` account and working-directory-mutation caveats in
  [Shared behavior](index.md#shared-behavior) — worth pinning `gh_token_cmd` to a specific
  account if you juggle multiple `gh` logins.
