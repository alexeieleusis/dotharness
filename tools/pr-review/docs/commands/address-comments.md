# address-comments

`address-comments` scans open pull requests you authored or are assigned to for reviewer feedback: unresolved inline review threads, review-level comments, and non-bot issue comments. For each, the configured AI backend decides whether it warrants a change, makes the smallest fix if so, commits, and replies on GitHub.

Unlike `self-review` or `review-requested`, which *produce* review comments, `address-comments` *consumes* them. It's the runner you schedule (or run by hand) to keep PRs moving after humans (or bots) have left feedback on them.

## Usage
```
harness run [--config PATH] [--verbose] address-comments
```
- `--config PATH` — path to the `.harness.toml` config file. The default is `./.harness.toml`, resolved relative to the current directory. This option belongs to the `run` group, so it must precede `address-comments` on the command line.
- `--verbose` — enables DEBUG-level logging and mirrors log output to stdout even when stdout isn't a TTY. Logs are always written to `~/.local/share/dotharness/logs/address-comments/<date>.log`.

## What it does

1. Acquires an exclusive file lock keyed on the resolved `repo.working_dir` path (not `repo.name`/`repo_slug` — see [Shared behavior](index.md#shared-behavior)). The lock is shared with the other four commands. A second concurrent invocation against the same working directory — running this or any other command — exits immediately with an error instead of waiting.
2. Resolves a GitHub token via `harness.gh_token_cmd`. It builds the subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
3. Lists the open PRs to check: `gh pr list --repo <repo.name> --author @me ...` and `gh pr list --repo <repo.name> --assignee @me ...`. Both use the same `--state open --json number,headRefName,isDraft --limit 500` flags. The results are merged and deduplicated by PR number, then sorted ascending.
4. Loads the single prompt template `pr-review/address-comment.md` from `harness.knowledge_dir`. It constructs a `Backend` for `harness.backend` (`opencode` or `claude`), with `GITHUB_TOKEN` merged into its environment.
5. If `repo.opencode_dir` is set, it computes that path relative to `repo.working_dir` (`plugin_prefix`). That path is used later to restrict which inline comments are considered.
6. Detaches HEAD and records the current commit so it can restore the working tree after each PR.
7. For each PR, in ascending order:
   - Skips it if it's a draft.
   - Calls a GraphQL query for unresolved review threads. If any thread is unresolved, the PR has "pending feedback." Otherwise, it falls back to checking for any issue/timeline comment not authored by `github-actions[bot]` or `dependabot[bot]`. If neither check finds anything, the PR is skipped entirely — no checkout, no backend calls.
   - Resolves the current `gh` user's login the first time it's needed (`gh api user --jq .login`), then reuses it for the rest of the run.
   - Checks whether the current user is currently a requested reviewer on this PR, before doing anything else with it.
   - Fetches and checks out the PR's head branch (using `git checkout -B` to `origin/<branch>` — see [Shared behavior](index.md#shared-behavior)).
   - Fetches all comments via `scripts/pr-comments.py fetch --pr <N>` and reads the JSON it caches at `~/.harness/cache/pr-<N>-comments.json`. The actionable set is:
     - all inline (review-thread) comments,
     - all PR-level review comments, and
     - issue comments whose author doesn't end in `[bot]` and whose body doesn't contain `[` followed by `bot]` (case-insensitive).
   - If a second GraphQL query (mapping unresolved threads to their comment IDs) succeeds, inline comments are further filtered to only those in a still-unresolved thread. If that query fails, this filter is skipped and inline comments are left as fetched.
   - Before the ordinary "already answered" check, it pulls out any inline comment whose most recent thread reply carries a `[focused-review-bot]` marker **and** has a `+1` reaction from the current user. This selection is programmatic (code-level), not left to the backend, because a detailed marker reply reads like a finished writeup, and an LLM asked to judge "has this been addressed?" can (and did) conclude it already had been. Each selected thread is re-pointed at that reply as the actual comment to address: the reply's body/author/ID become the comment's body/author/ID (so the backend's own reply threads correctly). File path, line, and diff context are inherited from the parent thread. See Notes below.
   - Drops the remaining comments that look already answered by this account. Those are inline comments whose most recent thread reply was authored by the current user, or issue comments authored by the current user. (Review-level comments have no such check — see Notes.) An inline thread with a `[focused-review-bot]` marker reply that hasn't been reacted to yet also falls through to this check. It's correctly read as "the current user (this account) already replied" and stays excluded until it's approved.
   - If `repo.opencode_dir` is configured, it further restricts inline comments to those whose file path falls under that subdirectory. Review and issue comments are unaffected by this filter.
   - If nothing survives all the filtering, the PR is skipped.
   - Otherwise, for each remaining comment, in order:
     - Builds a prompt from the `address-comment.md` template, comment-specific details, the PR number, and the repo name. The comment-specific details are:
       - inline comments: file/line, comment ID, author, URL, body, diff context, and thread replies
       - review comments: ID/author/state/URL/body
       - issue comments: ID/author/URL/body
       For a selected focused-review comment, the prompt tells the backend to skip the template's Step 0 (act/reply-only/skip triage) entirely, because the programmatic selection already decided this needs a real fix. The prompt also includes the original automated finding as background-only context, explicitly labeled as not the thing to implement.
     - Runs the configured backend (see `harness.backend` in [Configuration](../configuration.md)) against the checked-out working directory (or `opencode_dir`, if configured). Per the template's own instructions, the backend decides for itself whether an ordinary comment needs a code change, a reply-only response, or nothing at all (e.g. "LGTM"-style noise is skipped outright). If it acts, it stages only the files it touched, commits with a message that links back to the comment, and posts the reply to GitHub itself — via the inline-reply API for inline comments, `gh api .../issues/<N>/comments` for issue comments, or `gh pr comment` for review comments. None of that (commit or GitHub reply) is code in this runner. It all happens inside the backend's own tool use.
     - If the backend invocation raises (e.g. a timeout after its internal retry), the error is logged and the loop moves to the next comment for this PR. Nothing is pushed for that comment.
     - Otherwise, the runner itself runs `git push origin <branch>` right after the backend returns. If the push succeeds, it moves to the next comment. If it fails, a warning is logged and the **remaining comments for this PR are abandoned for this run**. The per-comment loop breaks, but processing continues with the next PR.
   - Any other exception while processing the PR is caught and logged. The run moves on to the next PR regardless.
   - Re-adds the user as a requested reviewer if they were one before this PR was processed, no matter what happened above. Replying to (or fixing) a comment can submit a review via the GitHub API as a side effect. That clears the submitter from the PR's requested-reviewer list. It would otherwise hide the PR from `review-requested`'s search for the rest of a `run all` cycle. This step runs even if an exception was raised above.
   - Always restores the working tree to the SHA recorded in step 6 before moving to the next PR.
8. After all PRs, it leaves the working directory checked out (detached) at `origin/main`.

## Configuration

Only these `.harness.toml` fields affect `address-comments`. See [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode`/`claude`) evaluates and addresses each comment |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation (one per actionable comment) |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/address-comment.md`, the prompt template for this runner |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for git, `gh`, and the backend subprocess |
| `repo.name` | GitHub repo (`org/repo`) queried via `gh` |
| `repo.working_dir` | Local git checkout that gets detached, fetched, and checked out branch-by-branch. Its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |
| `repo.opencode_dir` | If set: passed to the backend as its `--dir`, and used to restrict inline comments to that subdirectory |
| `address_comments.enabled` | Turns the command on. `false` skips the run entirely (default `true`) |
| `address_comments.trusted_commenters` | Restricts which comment authors are considered at all. `"*"` (default) considers everyone |

Fields this runner does **not** read: `harness.review_knowledge_file`, `repo.subdir[]` (any of its fields), and the entire `[vibe_heal]` section.

## State and idempotency

`address-comments` does not persist state between runs. Unlike `review-prs` and `self-review`, there is no "last processed PR/comment" file. `harness state reset address-comments` is not supported for this command.

Instead, it avoids reprocessing using only live signals read from GitHub on each run:
- A PR is skipped up front unless it currently has an unresolved review thread or a non-bot issue comment (the pending-feedback check).
- Inline comments are further narrowed to ones still in an unresolved thread. This is best-effort: if the GraphQL lookup fails, the narrowing is skipped, and previously-resolved inline comments are *not* excluded that run.
- A comment is treated as already handled if the current `gh` user's login is the author of the issue comment, or the author of the most recent reply in an inline comment's thread. This is a live check against GitHub, not a local record. A `[focused-review-bot]` marker reply is an exception carved out *before* this check runs. It's only "the current user's reply" in the sense that this account posted it. It isn't a completion — it's an unimplemented fix spec. Once it has a `+1` from the current user, it is pulled out and addressed. Until then, it correctly falls through to this same live check and stays excluded.
- The prompt template itself asks the backend to check for existing thread replies and skip re-fixing (but still acknowledge) anything that looks already addressed. This is an additional, backend-side layer of dedup on top of the code-level filters above. The template is explicit about what "already addressed" means: a reply pointing at a real, already-landed change (e.g. an "Addressed in `<commit-url>`" reply), not merely a reply that *describes* a proposed fix in detail. For marker replies specifically, the code-level focused-review selection above no longer leaves that distinction to the backend to get right. It still matters for ordinary human/bot comments.

Because none of this is state-file-based, resolving a thread on GitHub (or having the account reply, or making the account approve nothing relevant here) is what "marks it done". There's no way to force a re-run of an already-answered comment other than manually reopening/unresolving the thread or removing the account's reply.

## Notes

- **Review-level comments have no reply-based dedup in code.** The `our_login`-based filter only checks inline-comment thread replies and issue-comment authorship. A PR-level review comment (`type: review`) is refetched and resent to the backend on every run for as long as it exists, unless the backend decides (per the template's Step 0) that it's noise and does nothing. A real review comment with no reply from the account will keep being retried indefinitely.
- **Push happens per comment, not per PR.** The runner pushes immediately after each comment's backend invocation finishes successfully, so progress is saved incrementally. If a push fails partway through a PR's comment list, the earlier commits for that PR are already on `origin`. The remaining comments in that PR are skipped for this run (the comment loop breaks) rather than retried later in the same invocation.
- Push is a plain `git push origin <branch>` — never force-pushed.
- Bot filtering only applies to issue/timeline comments. It happens in two places with slightly different rules:
  - The comment-fetch step drops any issue comment whose author ends in `[bot]` or whose body contains a `[` character followed by `bot]` (case-insensitive).
  - The pending-feedback check's own issue-comment check separately hardcodes excluding `github-actions[bot]` and `dependabot[bot]`.

  Inline and review-level comments are never bot-filtered by the runner itself. That judgment is left entirely to the backend via the template's Step 0.
- PRs you authored (`gh pr list --author @me`) and PRs assigned to you (`gh pr list --assignee @me`) are both considered — the two lists are merged and deduplicated by PR number. A PR you're merely a *requested reviewer* on (not assigned) is still never touched by this command. That's `review-requested`'s job.
- Draft PRs are always skipped. There's no config flag to include them.
- A backend exception for one comment (e.g. a timeout) is logged and only skips that comment. It doesn't stop the rest of the PR's comment list or the rest of the batch.
- Subject to the shared locking, `gh` account, and working-directory-mutation caveats in [Shared behavior](index.md#shared-behavior).
- **Focused-review-bot replies are always gated behind a `+1` reaction, and are addressed as themselves, not as a proxy for the original comment.** Normally, any inline thread whose last reply was posted by this account is skipped as "already answered." That's exactly backwards for a `[focused-review-bot]` marker reply, which is a *proposed* fix, not a completed one. Instead, an inline thread whose last reply carries the marker is only added to the actionable list once the current user has left a `+1` on that specific reply. This is checked live via the GitHub reactions API on every run, with no additional state file. If the reactions lookup itself fails, the thread is treated as not-yet-approved (fails closed). Once approved, the thread is addressed using the *marker reply's own* body/author/comment-ID, not the terse original finding (SonarQube, vibe-heal, etc.) that triggered `focused-review` in the first place. The original finding is still passed to the backend, but only as labeled background context. The backend is also told to skip its own act/reply-only/skip triage for these — the `+1` already made that call.
