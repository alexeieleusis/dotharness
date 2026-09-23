# self-review

`self-review` finds your own open pull requests on GitHub and has the configured AI backend review them, file by file, posting inline feedback and a summary — the same way a colleague would review your PR before you ask a human to look at it. It also runs two separate, PR-level passes once per PR each: a design/architecture review, and a requirement-traceability review comparing the diff against the PR's linked ticket (see [State and idempotency](#state-and-idempotency)). Unlike `review-prs` (which reviews other people's PRs) or `review-requested` (which reacts to explicit review requests), this command filters strictly by PR author: it only ever looks at PRs opened by the currently authenticated `gh` user (`--author @me`). Run it after pushing a PR, or on a schedule, to get an automated first pass before requesting human review.

## Usage
```
harness run [--config PATH] [--verbose] self-review
```

- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml` (resolved relative to the current directory). This option belongs to the `run` group, so it must come before `self-review` on the command line.
- `--verbose` — enables DEBUG-level logging and (in addition to the per-day log file) also streams logs to stdout even when not attached to a TTY. Logs are written to `~/.local/share/dotharness/logs/self-review/<date>.log` regardless of this flag.

## What it does

1. Acquires an exclusive file lock keyed on the resolved `repo.working_dir` path (not `repo.name`/`repo_slug` — see [Shared behavior](index.md#shared-behavior)), shared with the other four commands, so two invocations against the same working directory — `self-review` or any of the other four — can't run concurrently; a second run exits immediately with an error instead of racing the first.
2. Resolves a GitHub token by running `harness.gh_token_cmd` (default `gh auth token`) and builds a subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
3. Loads the set of PR numbers already recorded as reviewed from state, then lists the caller's own open PRs via `gh pr list --repo <repo> --author @me --state open --json number,url,headRefName,createdAt,closingIssuesReferences`, sorted by PR number. The last two fields (GitHub's own closing-keyword-derived issue links plus the PR's creation time) come back from this same call at no extra `gh` cost, and feed the traceability pass's ticket resolution below.
4. Loads the shared prompt templates `review-file.md`, `review-summary.md`, `review-design.md`, and `review-traceability.md` from `harness.knowledge_dir/pr-review/`, plus the optional `harness.review_knowledge_file` (appended to every prompt as an "Additional Review Guide" section).
5. Constructs the `Backend` (opencode or claude, per `harness.backend`) and records the repo's current `HEAD` as a detached commit, so the working tree can always be restored.
6. For each PR: whether the file+summary pipeline, the design-review pass, and the traceability-review pass each still need to run is decided independently (see [State and idempotency](#state-and-idempotency)) — a PR already fully reviewed for files/summary can still get a design or traceability pass, and vice versa, in any combination. If none of the three is outstanding, the PR is skipped entirely with no checkout. Otherwise:
   - If a prior comment on the PR already starts with a `[bot]osc-review` or `Review Summary` marker (i.e. it's already been reviewed, possibly by a previous run whose state write didn't happen), the file/summary part of the pipeline is marked reviewed in state and skipped for this PR — no new file/summary review is generated.
   - Whichever of the two cases above applies, the PR is then fetched and checked out (using the shared rebase/reset behavior — see [Shared behavior](index.md#shared-behavior)) and the diff against the PR's base branch computed — a still-outstanding design or traceability pass needs that diff too, even when the file/summary pass was just marked done above.
   - For each changed file, it builds a review prompt (file diff, or the whole file if it's new or the diff touches ≥75% of it) plus PR metadata, the PR description, and any matching vibe-heal static-analysis context, and runs the backend against it. The backend is responsible for producing/posting the actual inline PR review comments — this command supplies the prompt and repo checkout, not the GitHub API calls. The backend runs with unrestricted shell access (see [Security](#security)).
   - After all files, it builds one more prompt from `review-summary.md` (listing every file reviewed) and runs the backend once more to produce the overall PR summary/comment.
   - If the design pass hasn't already succeeded for this PR, it builds one PR-wide prompt from `review-design.md` (every changed file's diff, concatenated) and runs the backend once more. As defense-in-depth, before invoking, it also checks GitHub directly for an existing `<!-- osc-review-design -->`-marked comment — if found, the pass is recorded as done without a new backend call.
   - If the traceability pass hasn't already succeeded for this PR (same defense-in-depth GitHub check, for `<!-- osc-review-traceability -->`), it resolves the PR's linked ticket(s): primarily via `closingIssuesReferences`, each entry fetched with `gh issue view`; falling back, only if that finds nothing, to scanning issue-timeline comments posted within 5 minutes of the PR's `createdAt` (any author, including bots) for an issue reference. If neither mechanism resolves a ticket, it posts the `# Requirement Traceability\nNo linked ticket found...` PR-level comment directly — **no backend call** — and this outcome is terminal, recorded as done and never retried for this PR even if a ticket link is added later. Otherwise it builds one PR-wide prompt from `review-traceability.md` (the same diff concatenation as the design pass, plus the resolved ticket(s) and any early-comment context) and runs the backend once more.
   - The working tree is always restored to the recorded detached `HEAD` afterward (even on failure), before the next PR is processed.
   - The PR is only added to the reviewed set in state — and only then persisted to disk — if every backend invocation for the file/summary part (all files plus the summary) exited 0 without timing out. If any invocation fails or times out, the PR is left unmarked so the *entire* file list is retried from scratch on the next run. The design pass's and the traceability pass's own completions are each tracked in their own separate state field and are unaffected by this (or by each other).
   - Errors while processing a single PR (e.g. a git checkout failure) are logged and that PR is skipped; the loop continues with the remaining PRs.

## Configuration

Only these `.harness.toml` fields affect `self-review`; see [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode` or `claude`) runs the review, summary, design, and traceability prompts |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation (one per changed file, one for the summary, one for the design pass when it hasn't already succeeded, and one for the traceability pass when it hasn't already succeeded) |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/review-file.md`, `pr-review/review-summary.md`, `pr-review/review-design.md`, and `pr-review/review-traceability.md` prompt templates |
| `harness.review_knowledge_file` | Optional extra guidance appended to every prompt, if the path exists |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for both git subprocesses and the backend |
| `repo.name` | The GitHub repo (`owner/name`) queried via `gh` |
| `repo.working_dir` | Local git checkout used to fetch/checkout PR branches and diff files; its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |
| `repo.subdir[].path` | Only used to locate each subdir's `sonar-project.properties`, so a matching vibe-heal `review.md` (if one exists on disk) can be included as static-analysis context |

`[vibe_heal]` settings are **not** read by `self-review` itself — that section controls a separate analysis step elsewhere in the tool. `self-review` only opportunistically picks up whatever vibe-heal review file already exists on disk for the PR's branch and matching Sonar project key. `repo.opencode_dir` is also not used by this command.

## State and idempotency

State is stored at `~/.local/share/dotharness/state/<repo_slug>/self_review.json` and tracks:
- `version` — schema version (currently `1`)
- `reviewed_prs` — list of PR numbers whose file+summary review already succeeded (or already carries a `[bot]osc-review`/`Review Summary` comment)
- `partial_reviews` — per-PR list of files already successfully reviewed in a prior, still-incomplete run (see [Notes](#notes))
- `design_reviewed_prs` — list of PR numbers whose design-review pass already succeeded. This is **tracked completely independently of `reviewed_prs`** (and of `traceability_reviewed_prs`): a design-pass failure never blocks or resets `reviewed_prs`/`partial_reviews`, and a file/summary failure never blocks or resets `design_reviewed_prs`.
- `traceability_reviewed_prs` — list of PR numbers whose requirement-traceability pass already succeeded (including the terminal "no linked ticket found" outcome — see [What it does](#what-it-does)). Tracked completely independently of `reviewed_prs` and `design_reviewed_prs` for the same reason. A PR is only skipped entirely (no checkout at all) once it appears in *all three* lists.

PRs in `reviewed_prs` are skipped on subsequent runs, so re-running `self-review` is safe and only does work for PRs opened (or newly qualifying) since the last successful pass. To force everything to be re-reviewed, clear the state:
```
harness state reset self-review --config PATH [--yes]
```
This deletes `self_review.json` for the repo after an interactive confirmation (skipped with `--yes`).

## Notes

- A PR's file+summary review is only marked reviewed if every file's review and the final summary all succeeded in the same run; a single timed-out or failing file means the whole file/summary pipeline — including files that succeeded — gets re-sent to the backend next time (`partial_reviews` gives per-file credit within that retry, see below).
- **Cost implication:** When a file fails mid-PR, the `_review_files` loop continues processing all remaining files, then the summary also runs. On the next invocation, files already recorded in `partial_reviews` are skipped and only the remaining files plus the summary are retried. With expensive backends and a file that keeps failing, this still approaches 2x the normal cost for large PRs in the worst case. Consider setting `harness.backend_timeout_seconds` conservatively to avoid mid-run timeouts, and monitor `~/.local/share/dotharness/logs/self-review/` for timeout patterns.
- **The design pass's cost is separate from the above and does not multiply it.** It's exactly one backend invocation per PR, tracked in its own `design_reviewed_prs` state independent of `reviewed_prs`/`partial_reviews` — a failing design pass is retried on its own next run without re-triggering any file/summary work, and a failing file/summary retry never re-triggers an already-succeeded design pass.
- **The traceability pass's cost is likewise independent and self-contained.** At most one backend invocation per PR — zero when no linked ticket can be resolved at all, since the "no ticket" comment is posted directly — tracked in its own `traceability_reviewed_prs` state, decoupled from `reviewed_prs`/`partial_reviews` and from `design_reviewed_prs` in both directions. Extra `gh` cost is one `gh issue view` call per resolved ticket (typically one) plus, only when the closing-keyword mechanism finds nothing, one paginated early-comment-window fetch.
- The "already reviewed" check is comment-based, not state-based: it looks for any PR comment whose body starts with `[bot]osc-review` or `Review Summary` (after stripping leading `#`/spaces). The `[bot]` prefix on `osc-review` was chosen to reduce collision risk with organic comments. If a comment with one of these exact prefixes is posted manually (or by another tool), `self-review` will treat the PR as already reviewed and skip it. To recover from this, clear the state with `harness state reset self-review`.
- Subject to the shared `gh` account and working-directory-mutation caveats in
   [Shared behavior](index.md#shared-behavior) — worth pinning `gh_token_cmd` to a specific
   account if you juggle multiple `gh` logins.

## Security

The backend process runs with elevated privileges that create an undocumented command execution surface. `claude` is invoked with `--dangerously-skip-permissions`, which grants the AI model unrestricted shell access within the subprocess; `opencode` needs no equivalent flag (see [Existing mitigations](#existing-mitigations) below). In practice this means the backend can:

- Execute **any** shell command, not just the `gh api` calls needed to post review comments
- Read and write the full working directory tree, including `.git/` metadata and any files checked out during PR processing
- Access `GITHUB_TOKEN` from the subprocess environment, which is inherited from the parent process

A confused or adversarial model response could exfiltrate source code, modify repository files, or abuse the GitHub token to make unauthorized API calls.

### Existing mitigations

The following mitigations are already in place in `backend.py`:

- **`--disable-slash-commands` (claude only):** Disables slash commands that could trigger built-in actions beyond the prompt scope.
- **`--standalone` (opencode only):** `--dangerously-skip-permissions`/`--pure` no longer exist in opencode v2 (non-interactive `run` auto-approves regardless), so this runs a private per-invocation server instead of sharing the background service, preventing concurrent reviews from cross-talking through shared session state.
- **Plugin isolation for opencode (`assert_no_opencode_plugins`):** `--standalone` above says nothing about a globally-installed plugin acting independently of that per-invocation isolation — a gap opencode v1's removed `--pure` flag used to close. At the start of each batch (the first opencode invocation a `Backend` instance makes — batches are one per runner call, e.g. one `focused-review` run across many PRs/comments), `backend.py` runs `opencode plugin list`, scoped to the batch's own working directory, and aborts the whole run if it reports anything other than "No plugins found". It is not re-checked for the rest of that batch. **Caveat:** this is checked once per batch, not continuously — a plugin installed after the check (mid-batch, or mid-run) would still slip through. `opencode plugin list` has no `--standalone` equivalent, so this one call still goes through opencode's shared background service rather than a private one.
- **New process session (`start_new_session=True`):** Each backend runs in its own process group, allowing the tool to kill the entire tree on timeout via `killpg` + `SIGKILL`. After every run (not just a timed-out one), `backend.py` also polls that process group and blocks the next comment's backend run from starting until it's empty (bounded to 30s, then proceeds anyway with a warning) — added after a spawned `--standalone` server was still tearing down when the next comment's run started, a plausible way for a request to land on the wrong server and answer against the wrong project directory. **Caveat:** a backend that double-forks into its own session (common for daemonizing subprocess managers) can escape the process group entirely and keep running with access to the working directory and `GITHUB_TOKEN` — the process-group poll won't see it. The timeout path separately detects such escapees by command name and logs a warning, but does not attempt a secondary kill — containment is not guaranteed in this case.
- **Working tree restoration:** After each PR is processed, the working tree is reset to a recorded detached `HEAD`, limiting the persistence of any file mutations.

### Mitigations to evaluate

The following have not been implemented but reduce the attack surface further:

- **`--disable-slash-commands` for opencode:** Currently applied only to claude; the equivalent flag for opencode would further restrict built-in actions.
- **Environment sanitization:** Stripping `GITHUB_TOKEN` and other sensitive env vars from the backend subprocess, passing only the tokens the AI actually needs for its specific task. This would require restructuring how the backend receives its GitHub credentials (e.g., writing the token to a temporary file the backend reads, rather than inheriting it from the environment).
- **Sandboxed execution:** Running the backend in a restricted container, namespace, or `firejail` profile to isolate filesystem and network access.
- **Read-only checkout:** Checking out PR branches with a read-only filesystem mount, though this would prevent the backend from writing files needed for its prompt files.
