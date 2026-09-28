# self-review

`self-review` finds your own open pull requests on GitHub and has the configured AI backend review them, file by file. The backend posts inline feedback and a summary, the same way a colleague would review your PR before you ask a human to look at it. It also runs two separate PR-level passes, each once per PR: a design/architecture review, and a requirement-traceability review comparing the diff against the PR's linked ticket (see [State and idempotency](#state-and-idempotency)). An optional third PR-level pass — the regret review — runs on top of these when `[regret_review].enabled` is `true` (off by default). Unlike `review-prs` (which reviews other people's PRs) or `review-requested` (which reacts to explicit review requests), this command filters by PR author only: it only reviews PRs opened by the currently authenticated `gh` user (`--author @me`). Run it after pushing a PR, or on a schedule, to get an automated first pass before requesting human review.

## Usage
```
harness run [--config PATH] [--verbose] self-review
```

- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml` (resolved relative to the current directory). This option belongs to the `run` group, so it must come before `self-review` on the command line.
- `--verbose` — enables DEBUG-level logging. It also streams logs to stdout in addition to the per-day log file, even when not attached to a TTY. Logs are written to `~/.local/share/dotharness/logs/self-review/<date>.log` regardless of this flag.

## What it does

1. Acquires an exclusive file lock keyed on the resolved `repo.working_dir` path (not `repo.name`/`repo_slug` — see [Shared behavior](index.md#shared-behavior)). The lock is shared with the other four commands. Two invocations against the same working directory, whether `self-review` or any of the other four, can't run concurrently. A second run exits immediately with an error instead of racing the first.
2. Resolves a GitHub token by running `harness.gh_token_cmd` (default `gh auth token`). It builds the subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
3. Loads the set of PR numbers already recorded as reviewed from state. It then lists the caller's own open PRs via `gh pr list --repo <repo> --author @me --state open --json number,url,headRefName,createdAt,closingIssuesReferences`, sorted by PR number. The last two fields — GitHub's own closing-keyword-derived issue links and the PR's creation time — are returned by the same call at no extra `gh` cost. They feed the traceability pass's ticket resolution below.
4. Loads the shared prompt templates `review-file.md`, `review-summary.md`, `review-design.md`, and `review-traceability.md` from `harness.knowledge_dir/pr-review/`. It also loads the optional `harness.review_knowledge_file`, appended to every prompt as an "Additional Review Guide" section.
5. Constructs the `Backend` (opencode or claude, per `harness.backend`). It records the repo's current `HEAD` as a detached commit, so the working tree can always be restored.
6. For each PR, it decides independently whether the file+summary pipeline, the design-review pass, the traceability-review pass, and (when enabled) the regret-review pass each still need to run (see [State and idempotency](#state-and-idempotency)). A PR already fully reviewed for files/summary can still get a design or traceability pass, and vice versa, in any combination. If none of the four is outstanding, the PR is skipped entirely with no checkout. Otherwise:
   - If a prior comment on the PR already starts with the `[bot]osc-review` or `Review Summary` marker, the PR was already reviewed (possibly by a previous run whose state write didn't happen). The file/summary part of the pipeline is marked reviewed in state and skipped for this PR. No new file/summary review is generated.
   - Whichever of the two cases above applies, the PR is then fetched and checked out (using the shared rebase/reset behavior — see [Shared behavior](index.md#shared-behavior)). It computes the diff against the PR's base branch. A still-outstanding design or traceability pass needs that diff too, even when the file/summary pass was just marked done above.
   - For each changed file, it builds a review prompt from the file diff (or the whole file, if the file is new or the diff touches ≥75% of it), plus PR metadata, the PR description, and any matching vibe-heal static-analysis context. It then runs the backend against that prompt. The backend produces and posts the actual inline PR review comments. This command supplies the prompt and repo checkout, not the GitHub API calls. The backend runs with unrestricted shell access (see [Security](#security)).
   - After all files, it builds one more prompt from `review-summary.md`, listing every file reviewed. It runs the backend once more to produce the overall PR summary/comment.
   - If the design pass hasn't already succeeded for this PR, it builds one PR-wide prompt from `review-design.md` (every changed file's diff, concatenated) and runs the backend once more. As defense in depth, before invoking the backend, it also checks GitHub directly for an existing comment marked `<!-- osc-review-design -->`. If it finds one, it records the pass as done without a new backend call.
   - If the traceability pass hasn't already succeeded for this PR (same defense in depth GitHub check, for `<!-- osc-review-traceability -->`), it resolves the PR's linked ticket(s), primarily via `closingIssuesReferences`, fetching each entry with `gh issue view`. If that finds nothing, it scans issue-timeline comments posted within 5 minutes of the PR's `createdAt` (any author, including bots) for an issue reference instead. If neither mechanism resolves a ticket, it posts the `# Requirement Traceability\nNo linked ticket found...` PR-level comment directly, with **no backend call**. That outcome is terminal: it is recorded as done and never retried for this PR, even if a ticket link is added later. Otherwise, it builds one PR-wide prompt from `review-traceability.md` (the same diff concatenation as the design pass, plus the resolved ticket(s) and any early-comment context) and runs the backend once more.
   - When `[regret_review].enabled` is `true` (off by default), the optional regret-review pass can also run on small, bugfix-shaped PRs: it traces this PR's changed lines back to the commit/PR that introduced them and, when that introducing PR carried a review comment on the same change that the backend judges would have prevented this bug, posts one PR-level comment linking back to those comments (one backend invocation per PR, only when candidates exist; see [`../configuration.md`](../configuration.md#regret_review)).
   - It always restores the working tree to the recorded detached `HEAD` afterward, even on failure, before the next PR is processed.
   - It adds the PR to the reviewed set in state, and persists it to disk, only if every backend invocation for the file/summary part (all files plus the summary) exited 0 without timing out. If any invocation fails or times out, it leaves the PR unmarked, so the *entire* file list is retried from scratch on the next run. The design and traceability passes are each tracked in their own separate state field. Their completions are unaffected by this rule or by each other.
   - If an error occurs while processing a single PR (e.g. a git checkout failure), it logs the error and skips that PR. The loop continues with the remaining PRs.

## Configuration

Only these `.harness.toml` fields affect `self-review`. See [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode` or `claude`) runs the review, summary, design, and traceability prompts, and — only when `[regret_review].enabled` is `true` — the regret-judgment prompt |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation (one per changed file, one for the summary, one for the design pass when it hasn't already succeeded, one for the traceability pass when it hasn't already succeeded, and — only when `[regret_review].enabled` is `true` — one for the regret pass when it hasn't already succeeded) |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/review-file.md`, `pr-review/review-summary.md`, `pr-review/review-design.md`, `pr-review/review-traceability.md`, and — only when `[regret_review].enabled` is `true` — `pr-review/review-regret.md` prompt templates |
| `harness.review_knowledge_file` | Optional extra guidance appended to every prompt, if the path exists |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for both git subprocesses and the backend |
| `repo.name` | The GitHub repo (`owner/name`) queried via `gh` |
| `repo.working_dir` | Local git checkout used to fetch/checkout PR branches and diff files. Its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |
| `repo.subdir[].path` | Only used to locate each subdir's `sonar-project.properties`, so a matching vibe-heal `review.md` (if one exists on disk) can be included as static-analysis context |

`[vibe_heal]` settings are **not** read by `self-review` itself — that section controls a separate analysis step elsewhere in the tool. `self-review` only opportunistically includes a vibe-heal review file that already exists on disk for the PR's branch and a matching Sonar project key. `repo.opencode_dir` is also not used by this command.

## State and idempotency

State is stored at `~/.local/share/dotharness/state/<repo_slug>/self_review.json` and tracks:
- `version` — schema version (currently `1`)
- `reviewed_prs` — list of PR numbers whose file+summary review already succeeded (or already carries a `[bot]osc-review`/`Review Summary` comment)
- `partial_reviews` — per-PR list of files already successfully reviewed in a prior, still-incomplete run (see [Notes](#notes))
- `design_reviewed_prs` — list of PR numbers whose design-review pass already succeeded. This is **tracked completely independently of `reviewed_prs`** (and of `traceability_reviewed_prs`): a design-pass failure never blocks or resets `reviewed_prs`/`partial_reviews`, and a file/summary failure never blocks or resets `design_reviewed_prs`.
- `traceability_reviewed_prs` — list of PR numbers whose requirement-traceability pass already succeeded (including the terminal "no linked ticket found" outcome — see [What it does](#what-it-does)). It is tracked completely independently of `reviewed_prs` and `design_reviewed_prs` for the same reason.
- `regret_reviewed_prs` — list of PR numbers whose regret-review pass already succeeded (including the silent "no confirmed regret" outcome, when the backend judged every candidate `NO` and there was nothing to post). The pass only runs when `[regret_review].enabled` is `true` (off by default), and it is tracked completely independently of `reviewed_prs`, `design_reviewed_prs`, and `traceability_reviewed_prs` for the same reason: a regret-pass failure never blocks or resets the others, and vice versa. A PR whose candidate search finds nothing to judge is deliberately not added here: that skip ends only the current run, so a later PR update gets a fresh candidate search. A PR is only skipped entirely (no checkout at all) once it appears in *all four* lists — counting `regret_reviewed_prs` only when the pass is enabled.

PRs in `reviewed_prs` are skipped on subsequent runs. So re-running `self-review` is safe: it only does work for PRs opened (or newly qualifying) since the last successful pass. To force everything to be re-reviewed, clear the state:
```
harness state reset self-review --config PATH [--yes]
```
This deletes `self_review.json` for the repo after an interactive confirmation (skipped with `--yes`).

## Notes

- A PR's file+summary review is only marked reviewed if every file's review and the final summary all succeeded in the same run. A single timed-out or failing file means the whole file/summary pipeline, including files that succeeded, gets re-sent to the backend next time. `partial_reviews` gives per-file credit within that retry, see below.
- **Cost implication:** When a file fails mid-PR, the `_review_files` loop continues processing all remaining files, then the summary also runs. On the next invocation, files already recorded in `partial_reviews` are skipped, and only the remaining files plus the summary are retried. With expensive backends and a file that keeps failing, this still approaches 2x the normal cost for large PRs in the worst case. Consider setting `harness.backend_timeout_seconds` conservatively to avoid mid-run timeouts. Monitor `~/.local/share/dotharness/logs/self-review/` for timeout patterns.
- **The design pass's cost is separate from the above and does not multiply it.** It's exactly one backend invocation per PR, tracked in its own `design_reviewed_prs` state, independent of `reviewed_prs`/`partial_reviews`. A failing design pass is retried on its own next run, without re-triggering any file/summary work. A failing file/summary retry never re-triggers an already-succeeded design pass.
- **The traceability pass's cost is likewise independent and self-contained.** At most one backend invocation per PR. Zero when no linked ticket can be resolved at all, since the "no ticket" comment is posted directly. It is tracked in its own `traceability_reviewed_prs` state, decoupled from `reviewed_prs`/`partial_reviews` and from `design_reviewed_prs` in both directions. The extra `gh` cost is one `gh issue view` call per resolved ticket (typically one), plus one paginated early-comment-window fetch only when the closing-keyword mechanism finds nothing.
- **The regret pass's cost is likewise independent, and gated.** It runs only when `[regret_review].enabled` is `true` (off by default), and at most one backend invocation per PR — zero when the candidate search (blame → introducing-PR lookup → comment match) finds nothing to judge, which posts nothing and is not marked done, so it may repeat on a later run. It is tracked in its own `regret_reviewed_prs` state, decoupled from `reviewed_prs`/`partial_reviews` and from the design and traceability passes in both directions. Its `git blame`/`gh api`/comment-fetch cost is bounded by `[regret_review].max_diff_lines` before any blame work, and the whole per-PR sequence of `git`/`gh` calls is additionally capped by `[regret_review].regret_review_timeout`.
- The "already reviewed" check is comment-based, not state-based: it looks for any PR comment whose body starts with `[bot]osc-review` or `Review Summary` (after stripping leading `#`/spaces). The `[bot]` prefix on `osc-review` was chosen to reduce collision risk with organic comments. If a comment with one of these exact prefixes is posted manually (or by another tool), `self-review` will treat the PR as already reviewed and skip it. To recover from this, clear the state with `harness state reset self-review`.
- This command is subject to the shared `gh` account and working-directory-mutation caveats in [Shared behavior](index.md#shared-behavior). Pin `gh_token_cmd` to a specific account if you have multiple `gh` logins.

## Security

The backend process runs with elevated privileges that create an undocumented command execution surface. `claude` is invoked with `--dangerously-skip-permissions`. That flag grants the AI model unrestricted shell access within the subprocess. `opencode` needs no equivalent flag (see [Existing mitigations](#existing-mitigations) below). In practice, the backend can:

- Execute **any** shell command, not just the `gh api` calls needed to post review comments
- Read and write the full working directory tree, including `.git/` metadata and any files checked out during PR processing
- Access `GITHUB_TOKEN` from the subprocess environment, which is inherited from the parent process

A confused or adversarial model response could exfiltrate source code, modify repository files, or abuse the GitHub token to make unauthorized API calls.

### Existing mitigations

The following mitigations are already in place in `backend.py`:

- **`--disable-slash-commands` (claude only):** Disables slash commands that could trigger built-in actions beyond the prompt scope.
- **`--standalone` (opencode only):** `--dangerously-skip-permissions`/`--pure` no longer exist in opencode v2 (non-interactive `run` auto-approves regardless). This flag runs a private per-invocation server instead of sharing the background service. That prevents concurrent reviews from cross-talking through shared session state.
- **Plugin isolation for opencode (`assert_no_opencode_plugins`):** `--standalone` above says nothing about a globally installed plugin acting independently of that per-invocation isolation. That is a gap that opencode v1's removed `--pure` flag used to close. Batches are one per runner call, for example one `focused-review` run across many PRs/comments. At the start of each batch, which is the first opencode invocation a `Backend` instance makes, `backend.py` runs `opencode plugin list`, scoped to the batch's own working directory. It aborts the whole run if `opencode plugin list` reports anything other than "No plugins found". It does not re-check for the rest of that batch. **Caveat:** the check runs once per batch, not continuously. A plugin installed after the check (mid-batch, or mid-run) would still not be caught. `opencode plugin list` has no `--standalone` equivalent, so this one call still goes through opencode's shared background service rather than a private one.
- **New process session (`start_new_session=True`):** Each backend runs in its own process group, so the tool can kill the entire tree on timeout via `killpg` + `SIGKILL`. After every run (not just a timed-out one), `backend.py` also polls that process group and blocks the next comment's backend run from starting until the group is empty. The poll is bounded to 30s. After that, it proceeds anyway with a warning. That check was added after a spawned `--standalone` server was still shutting down when the next comment's run started. That was a plausible way for a request to reach the wrong server and answer against the wrong project directory. **Caveat:** a backend that double-forks into its own session (common for daemonizing subprocess managers) can escape the process group entirely and keep running with access to the working directory and `GITHUB_TOKEN`. The process-group poll will not see it. The timeout path separately detects such escapees by command name and logs a warning, but does not attempt a secondary kill. Containment is not guaranteed in this case.
- **Working tree restoration:** After each PR is processed, the working tree is reset to a recorded detached `HEAD`, limiting the persistence of any file mutations.

### opencode `--standalone` project misrouting (root cause confirmed and fixed)

Observed in production (PR #9670, #9676, #9664, and others): an opencode `--standalone` backend process was `Popen`'d with the correct `cwd`. It was later found (via `lsof -p <pid> | grep cwd`) operating out of a *different* directory. Concretely, it was operating wherever the harness's own long-running loop script (`runPrReviewOpencode.sh`) happened to `cd` into before invoking it (`cd "$HARNESS_DIR"`, i.e. the dotharness checkout itself).

**Root cause:** `_build_env()` copied `os.environ` verbatim, which includes `PWD` as inherited from that wrapper script's `cd`. That `PWD` is stale relative to the `cwd` a given `Backend.run()` call actually uses. `subprocess.Popen(cwd=...)` correctly `chdir`s the child process at the OS level, but never syncs the `PWD` env var to match it. A direct reproduction confirmed the behavior in opencode v2: spawn opencode with a stale `PWD` and a different `cwd`, then check `lsof`. opencode v2 trusted the stale `PWD` over its own `getcwd()` and re-`chdir`'d itself to match it, silently operating out of the wrong project for the rest of the session. This is why the bug never reproduced under any manual `cd <target> && opencode run ...` test. An interactive shell's `cd` always keeps `PWD` correct, so the divergence only appeared when launched from a script or process with a stale `PWD`.

**Fix:** `_build_env(cwd)` now explicitly sets `env["PWD"] = cwd`, keeping it in sync with the directory `Popen` actually uses regardless of what the harness process itself inherited.

Three additional mitigations were added alongside the fix and kept as defense in depth, since they don't depend on `PWD` being the *only* possible cause:

- **`--auto`:** opencode v2's actual replacement for the removed `--dangerously-skip-permissions` ("auto-approve permissions that are not explicitly denied"). The original v2 migration assumed non-interactive `run` auto-approves without it. That is true for edit/bash, but the global `opencode.json` defaults `external_directory` permission to `"ask"` for anything not explicitly allowlisted.
- **Global opencode serialization:** every opencode backend invocation, across every config/tool on this host, is serialized behind one `flock` (`_OPENCODE_GLOBAL_LOCK_KEY` in `backend.py`), not just within one runner's batch. The cost is throughput: opencode-backed runs across all tools now queue behind each other host-wide.
- **`_CwdDivergenceMonitor` (hard safety net):** while a backend process runs, a background thread polls its actual OS-level cwd via `lsof` every few seconds. If it no longer matches the directory it was launched in, the process is killed immediately and `Backend.run()` raises `BackendCwdDivergedError` instead of returning. So a misrouted run (from this or any other future cause) can never produce a trusted comment or commit. Each runner already treats a `backend.run()` exception as "skip this comment, log it, move on," so this fails closed rather than posting bad output.

### Mitigations to evaluate

The following have not been implemented but reduce the attack surface further:

- **`--disable-slash-commands` for opencode:** Currently applied only to claude. The equivalent flag for opencode would further restrict built-in actions.
- **Environment sanitization:** Stripping `GITHUB_TOKEN` and other sensitive env vars from the backend subprocess, passing only the tokens the AI actually needs for its specific task. This would require restructuring how the backend receives its GitHub credentials (e.g., writing the token to a temporary file the backend reads, rather than inheriting it from the environment).
- **Sandboxed execution:** Running the backend in a restricted container, namespace, or `firejail` profile to isolate filesystem and network access.
- **Read-only checkout:** Checking out PR branches with a read-only filesystem mount, though this would prevent the backend from writing files needed for its prompt files.
