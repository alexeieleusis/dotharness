# review-prs

Runs automated static-analysis review (via the external `vibe_heal` tool) against open, non-draft pull
requests in a repo, posting results as PR comments. It is a batch/sweep command — meant to run on a schedule
(e.g. via `harness schedule install review-prs`) so that every open PR gets vibe_heal/SonarQube-style feedback
without anyone triggering it by hand. Unlike `review-requested` or `self-review`, this command does not invoke
harness's own AI backend (opencode/claude) directly — it shells out to `vibe_heal`, which does its own analysis
and posts its own comments.

## Usage
```
harness run [--config PATH] [--verbose] review-prs [--pr PR_URL]
```
- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml` (resolved relative
  to the current directory) if omitted.
- `--verbose` — enables DEBUG-level logging and mirrors log output to stdout (normally only written to the
  per-day log file under `~/.local/share/dotharness/logs/review-prs/`).
- `--pr PR_URL` — optional. When given, runs the vibe_heal pipeline against only that one PR (the PR number is
  parsed as the last path segment of the URL) instead of discovering open PRs automatically. This mode does
  not consult the per-PR reviewed-SHA record to decide eligibility, and does not update it afterward — so the
  same PR can be re-run any number of times (e.g. to pick up a config change or re-run after a transient
  failure) without affecting which PRs future automatic runs pick up. It still performs the prune-stale-projects
  step (step 3a below, if `vibe_heal.prune_projects_enabled` is `true`) and the baseline analysis step (step 3b
  below), the latter of which reads and may write `last_main_sha` in the state file. It also skips
  the draft and `vibe_heal.authors` filtering that the batch case applies — a single PR fetched this way is
  processed regardless of draft status or author.

## What it does

1. Acquires a per-repo lock (`repo_slug`), shared with the other four commands. If another
   run — `review-prs` or any of the other four — is already holding the lock for the same
   repo, the new invocation exits immediately with an error instead of waiting.
2. If `vibe_heal.enabled` is `false` in config, logs and exits — the whole command is a no-op.
3. Builds a subprocess environment: resolves a GitHub token via `harness.gh_token_cmd`, applies
   `harness.path_prepend` / `harness.env`.
3a. **Prune stale projects** (`prune_projects.run`): if `vibe_heal.prune_projects_enabled` is `true`, runs
   `vibe_heal prune-projects --yes --older-than <vibe_heal.prune_older_than_minutes>` once in each
   `repo.subdir` — each subdir supplies its own SonarQube project key via its own `.env.vibeheal` file, so
   there's no single repo-wide project key to prune against. `--yes` skips vibe_heal's own confirmation
   prompt, so matching stale projects (temp projects left behind by an interrupted vibe_heal run, with zero
   finished analyses, older than the threshold) are deleted immediately. A failure or timeout in one subdir
   (bounded by `vibe_heal.prune_projects_timeout`) is logged and does not stop pruning in the remaining
   subdirs, and never blocks baseline analysis or PR review below — this step is best-effort cleanup, not a
   prerequisite. No-op (not even attempted) if `prune_projects_enabled` is `false` (the default). Also a no-op —
   regardless of `prune_projects_enabled` — if `vibe_heal.enabled` is `false` (see step 2 above) or `repo.subdirs`
   is empty, since `_run_locked` returns before reaching this step in either case.
3b. **Baseline analysis** (`_run_base_analysis`): fetches `origin/main`, resolves its SHA, and checks the
   stored `last_main_sha` in the `vibe_heal.json` state file. If the SHA has changed since the last run,
   detaches HEAD, checks out `origin/main` (`--recurse-submodules`), and runs
   `vibe_heal review --baseline` in every `repo.subdir`. On success, persists the new `last_main_sha`. On any
   failure — fetch error, checkout error, or a failing subdir — the function returns `False` and the *entire*
   run aborts with only a log line (`"Base analysis failed; skipping PR review for this run"`). No PR comments
   are posted, so both batch and `--pr` invocations can silently do nothing if the baseline step fails. The
   `vibe_heal.vibe_heal_timeout` config value is reused as the timeout for each `--baseline` invocation.
   After the step completes (success or failure), the working directory is restored to its original state.
4. Looks up the current `gh` user's login.
5. Builds the list of PRs to process:
   - If `--pr` was given, resolves just that PR via `gh pr view` (`number`, `headRefName`, `baseRefName`,
     `headRefOid`).
   - Otherwise, lists open PRs via `gh pr list --repo <repo.name> --state open --limit 500`, filters to
     **not a draft** and author matching `vibe_heal.authors` (`"*"` or an explicit login list), then drops any
     PR that's ineligible per its `reviewed_shas` entry (see [State and idempotency](#state-and-idempotency)):
     either its current head commit (`headRefOid`) matches the SHA already recorded for it (unchanged since
     last successful review), or it changed but less than `vibe_heal.min_reanalysis_interval_hours` has passed
     since that last successful review. What's left is sorted ascending by number.
   - Before any checkout happens, the full open+author-matching PR-number set from this step is used to prune
     `reviewed_shas` of entries for PRs that are no longer open (closed/merged) — this runs even if the list
     of PRs left to process ends up empty.
6. If there are no PRs left to process, the command exits.
7. Detaches HEAD and records the current SHA, then processes each remaining PR in order:
   - Fetches and checks out the PR's head branch (see rebase/reset behavior in
     [Commands → Shared behavior](index.md#shared-behavior)).
   - Posts a one-time general marker comment on the PR (`[vibe-heal-bot]` marker) explaining that automated
     analysis comments follow, if not already posted.
   - Determines whether the current user is currently a requested reviewer on the PR.
   - Computes the PR's changed files — unless every configured `repo.subdir` is the repo root (`.`/`""`), in
     which case this is skipped and every subdir is treated as changed.
   - For each `repo.subdir`: skips it if it's not the root and none of the changed files fall under its path;
     otherwise runs its `pre_commands`, then `vibe_heal review --pr <N>` (with `--coverage` if the subdir has
     `coverage = true`), then `vibe_heal review --post --pr <N>` — this second step is what actually posts the
     analysis findings as PR comments. A failing (non-critical) pre-command is logged and skipped; a failing
     `critical` pre-command, or a failing `vibe_heal` invocation, stops processing for that subdir only — it
     does not raise, so it does not affect the rest of the PR's processing below.
   - If any subdir posted a review and the user was a requested reviewer before checkout, re-requests them as
     a reviewer — submitting a review via the GitHub API clears the submitter from the PR's requested-reviewer
     list, which would otherwise hide the PR from `review-requested`'s search for the rest of a `run all` cycle.
   - Unless running with `--pr`, records this PR's current head SHA and the current time in `reviewed_shas`
     **only if every subdir it was processed in succeeded** — this write happens immediately, right after this
     PR finishes, and does not depend on how any other PR in this batch turns out. A PR that fails (or
     partially fails across subdirs) is retried against the same head SHA on the next run.
   - A `FatalGitError` aborts processing of all remaining PRs in this run. Any other exception is logged and
     processing moves on to the next PR.
    - After each PR (success, failure, or exception), the working directory is restored to the SHA recorded in
      step 7.

## Configuration

Only the fields below affect this command. See [`../configuration.md`](../configuration.md) for the full schema.

| Field | Effect |
|---|---|
| `harness.gh_token_cmd` | Command used to obtain the GitHub token passed to `gh`/git subprocesses. |
| `harness.path_prepend` | Directories prepended to `PATH` for all subprocesses this runner spawns. |
| `harness.env` | Extra environment variables merged into the subprocess environment. |
| `repo.name` | GitHub repo (`org/repo`) queried via `gh pr list`/`gh pr view` and used for posting comments. |
| `repo.working_dir` | Local git working copy that gets checked out/fetched for each PR. |
| `repo.subdir[].path` | Directory (relative to `working_dir`) each `vibe_heal` invocation runs in; also used to filter which subdirs run for a given PR's changed files. |
| `repo.subdir[].pre_commands` | Commands run before `vibe_heal` in that subdir; a failing `critical` command aborts that subdir. |
| `repo.subdir[].coverage` | Adds `--coverage` to the per-PR `vibe_heal review` invocation. |
| `repo.subdir[].timeout` | Timeout (seconds) for each pre-command in that subdir. |
| `vibe_heal.enabled` | Master switch — command is a no-op unless `true`. |
| `vibe_heal.python` | Python interpreter used to invoke `vibe_heal` (`<python> -m vibe_heal ...`). |
| `vibe_heal.authors` | `"*"` (any author) or a list of GitHub logins; PRs from non-matching authors are filtered out of eligibility in the batch (non-`--pr`) case. |
| `vibe_heal.vibe_heal_timeout` | Timeout for the `vibe_heal review --pr` and `vibe_heal review --baseline` invocations. |
| `vibe_heal.vibe_heal_post_timeout` | Timeout for the `vibe_heal review --post --pr` invocation. |
| `vibe_heal.min_reanalysis_interval_hours` | Minimum time since a PR's last successful review before it becomes eligible again in batch mode, even if its head SHA has changed. Ignored with `--pr`. |
| `vibe_heal.prune_projects_enabled` | Master switch for the prune-stale-projects step (step 3a) — a no-op unless `true`. |
| `vibe_heal.prune_older_than_minutes` | Age threshold (minutes) passed as `vibe_heal prune-projects --older-than`. Ignored if `prune_projects_enabled` is `false`. |
| `vibe_heal.prune_projects_timeout` | Timeout for each subdir's `vibe_heal prune-projects` invocation. Ignored if `prune_projects_enabled` is `false`. |

Fields this runner does **not** read: `harness.backend`, `harness.backend_timeout_seconds`,
`harness.knowledge_dir`, `harness.review_knowledge_file`, `repo.opencode_dir` — those only matter to runners
that invoke harness's own AI backend directly.

## State and idempotency

State is stored at `~/.local/share/dotharness/state/<repo_slug>/vibe_heal.json` (`repo_slug` is `repo.name`
with `/` replaced by `-`). It tracks two fields:
- `reviewed_shas` — a map of PR number (as a string) to `{"sha": ..., "reviewed_at": ...}`, the head commit
  SHA and epoch-seconds timestamp of the PR's last *successful* review. A batch run skips a PR if either:
  its current `headRefOid` matches the recorded `sha` (nothing changed since the last review), or the head
  changed but fewer than `vibe_heal.min_reanalysis_interval_hours` have passed since `reviewed_at` (too soon
  to re-run, even though new commits landed). Each entry is written the moment that PR finishes successfully
  — independent of every other PR in the same run — and is removed once that PR is no longer open. This is
  what makes success durable even when some other PR in the same batch keeps failing (e.g. hangs and times
  out): a perpetually-broken PR only ever blocks itself, never the PRs discovered alongside it.
  State files written before this option existed store `reviewed_shas` values as plain SHA strings; these are
  transparently upgraded on read to `{"sha": <value>, "reviewed_at": 0}`, which makes them immediately eligible
  for re-review regardless of `min_reanalysis_interval_hours` (since their real last-reviewed time is unknown).
- `last_main_sha` — the SHA of `origin/main` last processed during baseline analysis (step 3b). When the
  current `origin/main` SHA differs from this value, the baseline analysis runs `vibe_heal review --baseline`
  in each subdir and then persists the new SHA. Both batch and `--pr` runs read and may write this field.

Run `harness state reset review-prs` (add `--yes` to skip the confirmation prompt) to delete this file, which
clears `reviewed_shas` and `last_main_sha` — the next run will re-consider every open PR from scratch and
re-run baseline analysis unconditionally.

`--pr` runs do not read or write `reviewed_shas`, but they do read and may write `last_main_sha` during the
baseline analysis step — see the `--pr` description under [Usage](#usage) and step 3b above.

## Notes

- **Draft PRs are always skipped** in the batch case, unconditionally — there is no config flag to include
  them. A PR passed explicitly via `--pr` is processed even if it's a draft.
- `reviewed_shas` is a per-PR "already reviewed, at this exact commit, at this time" record, not a monotonic
  watermark — a PR whose head SHA has changed becomes eligible again once `min_reanalysis_interval_hours` has
  passed since its last successful review, regardless of PR number. Only genuinely successful PRs get an
  entry; a failure leaves that PR eligible for retry on the next run (the interval only gates *successful*
  re-review of a PR that already has a recorded entry — a never-reviewed or previously-failed PR is always
  eligible).
- Checking out a PR's branch uses the shared rebase/reset behavior described in
  [Commands → Shared behavior](index.md#shared-behavior).
- The `[vibe-heal-bot]` general notice comment is idempotent: existing PR comments are checked for the marker
  text before posting, so re-running never duplicates it.
- The actual review content is produced and posted by the external `vibe_heal` CLI (`vibe_heal review --pr`
  then `vibe_heal review --post --pr`), not by harness itself — this command only orchestrates checkouts,
  invocation, and bookkeeping around it.
- `gh pr list` is capped at 500 open PRs per run.
- Errors while restoring the working directory to its pre-PR SHA are logged but not raised, so a failed
  restore won't crash the run.
