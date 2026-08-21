# address-comments

`address-comments` scans open pull requests you authored or are assigned to for reviewer feedback — unresolved inline review threads, review-level comments, and non-bot issue comments — and has the configured AI backend act on each one individually: deciding whether it warrants a change, making the smallest fix if so, committing, and replying back on GitHub. Unlike `self-review` or `review-requested` (which *produce* review comments), `address-comments` *consumes* them — it's the runner you schedule (or run by hand) to keep PRs moving after humans (or bots) have left feedback on them, whether or not you wrote the PR yourself.

## Usage
```
harness run [--config PATH] [--verbose] address-comments
```
- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml` (resolved relative to the current directory) if omitted. This option belongs to the `run` group, so it must precede `address-comments` on the command line.
- `--verbose` — enables DEBUG-level logging and also mirrors log output to stdout even when stdout isn't a TTY. Logs are always written to `~/.local/share/dotharness/logs/address-comments/<date>.log`.

## What it does

1. Acquires an exclusive file lock keyed on `repo_slug`, shared with the other four commands; a second concurrent invocation for the same repo — running this or any other command — exits immediately with an error instead of waiting.
2. Resolves a GitHub token via `harness.gh_token_cmd` and builds a subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
3. Lists open PRs to check: `gh pr list --repo <repo.name> --author @me ...` and `gh pr list --repo <repo.name> --assignee @me ...` (same `--state open --json number,headRefName,isDraft --limit 500` on both), merged and deduplicated by PR number, sorted ascending.
4. Loads the single prompt template `pr-review/address-comment.md` from `harness.knowledge_dir`, and constructs a `Backend` for `harness.backend` (`opencode` or `claude`), with `GITHUB_TOKEN` merged into its environment.
5. If `repo.opencode_dir` is set, computes its path relative to `repo.working_dir` (`plugin_prefix`) — used later to restrict which inline comments are considered.
6. Detaches HEAD and records the current commit so the working tree can be restored after each PR.
7. For each PR, in ascending order:
   - Skips it if it's a draft.
   - Calls a GraphQL query for unresolved review threads; if any thread is unresolved, the PR has "pending feedback." Otherwise it falls back to checking for any issue/timeline comment not authored by `github-actions[bot]` or `dependabot[bot]`. If neither check finds anything, the PR is skipped entirely — no checkout, no backend calls.
   - Resolves the current `gh` user's login the first time it's needed (`gh api user --jq .login`), then reuses it for the rest of the run.
    - Fetches and checks out the PR's head branch (using `git checkout -B` to `origin/<branch>` — see [Shared behavior](index.md#shared-behavior)).
   - Fetches all comments via `scripts/pr-comments.py fetch --pr <N>` and reads the JSON it caches at `~/.harness/cache/pr-<N>-comments.json`. The actionable set is: all inline (review-thread) comments, all PR-level review comments, and issue comments whose author doesn't end in `[bot]` and whose body doesn't contain `[` followed by `bot]` (case-insensitive).
   - If a second GraphQL query (mapping unresolved threads to their comment IDs) succeeds, inline comments are further filtered down to only those belonging to a still-unresolved thread; if that query fails, this filter is skipped and inline comments are left as fetched.
   - Before the ordinary "already answered" check, pulls out any inline comment whose most recent thread reply carries a `[focused-review-bot]` marker **and** has a `+1` reaction from the current user — this selection is programmatic (code-level), not left to the backend, precisely because a detailed marker reply reads like a finished writeup and an LLM asked to judge "has this been addressed?" can (and did) conclude it already had been. Each selected thread is re-pointed at that reply as the actual comment to address: the reply's body/author/ID become the comment's body/author/ID (so the backend's own reply threads correctly), while file path, line, and diff context are inherited from the parent thread — see Notes below.
   - Drops the remaining comments that look already answered by this account: inline comments whose most recent thread reply was authored by the current user, or issue comments authored by the current user. (Review-level comments have no such check — see Notes.) An inline thread with a `[focused-review-bot]` marker reply that hasn't been reacted to yet falls through to this check too, where it's correctly read as "the current user (this account) already replied" and stays excluded until it's approved.
   - If `repo.opencode_dir` is configured, further restricts inline comments to those whose file path falls under that subdirectory; review and issue comments are unaffected by this filter.
   - If nothing survives all the filtering, the PR is skipped.
   - Otherwise, for each remaining comment, in order:
     - Builds a prompt from the `address-comment.md` template plus comment-specific details (file/line, comment ID, author, URL, body, diff context, and thread replies for inline comments; ID/author/state/URL/body for review comments; ID/author/URL/body for issue comments), plus the PR number and repo name. For a selected focused-review comment, the prompt also tells the backend to skip the template's Step 0 (act/reply-only/skip triage) entirely — the programmatic selection already decided this needs a real fix — and includes the original automated finding as background-only context, explicitly labeled as not the thing to implement.
     - Runs the configured backend (see `harness.backend` in [Configuration](../configuration.md)) against the checked-out working directory (or `opencode_dir`, if configured). Per the template's own instructions, the backend decides for itself whether an ordinary comment needs a code change, a reply-only response, or nothing at all (e.g. "LGTM"-style noise is skipped outright); if it acts, it stages only the files it touched, commits with a message that links back to the comment, and posts the reply to GitHub itself — via the inline-reply API for inline comments, `gh api .../issues/<N>/comments` for issue comments, or `gh pr comment` for review comments. None of that (commit or GitHub reply) is code in this runner; it all happens inside the backend's own tool use.
     - If the backend invocation raises (e.g. a timeout after its internal retry), the error is logged and the loop moves to the next comment for this PR — nothing is pushed for that comment.
     - Otherwise, the runner itself runs `git push origin <branch>` right after the backend returns. If the push succeeds, it moves to the next comment; if it fails, a warning is logged and the **remaining comments for this PR are abandoned for this run** (the per-comment loop breaks, but processing continues with the next PR).
   - Any other exception while processing the PR is caught and logged; the run moves on to the next PR regardless.
   - The working tree is always restored to the SHA recorded in step 6 before moving to the next PR.
8. After all PRs, the working directory is left checked out (detached) at `origin/main`.

## Configuration

Only these `.harness.toml` fields affect `address-comments`; see [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `harness.backend` | Which AI backend (`opencode`/`claude`) evaluates and addresses each comment |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation (one per actionable comment) |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/address-comment.md`, the prompt template for this runner |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for git, `gh`, and the backend subprocess |
| `repo.name` | GitHub repo (`org/repo`) queried via `gh`, and the basis of the lock key (`repo_slug`) |
| `repo.working_dir` | Local git checkout that gets detached, fetched, and checked out branch-by-branch |
| `repo.opencode_dir` | If set: passed to the backend as its `--dir`, and used to restrict inline comments to that subdirectory |
| `address_comments.trusted_commenters` | Restricts which comment authors are considered at all; `"*"` (default) considers everyone |

Fields this runner does **not** read: `harness.review_knowledge_file`, `repo.subdir[]` (any of its fields), and the entire `[vibe_heal]` section — none of those affect `address-comments`.

## State and idempotency

`address-comments` does not persist state between runs — unlike `review-prs` and `self-review`,
there is no "last processed PR/comment" file, and `harness state reset address-comments` is not
supported for this command.

Instead, it avoids reprocessing using only live signals read from GitHub on each run:
- A PR is skipped up front unless it currently has an unresolved review thread or a non-bot issue comment (the pending-feedback check).
- Inline comments are further narrowed to ones still in an unresolved thread (best-effort — skipped if the GraphQL lookup fails, in which case previously-resolved inline comments are *not* excluded that run).
- A comment is treated as already handled if the current `gh` user's login is the author of the issue comment, or the author of the most recent reply in an inline comment's thread — this is a live check against GitHub, not a local record. A `[focused-review-bot]` marker reply is an exception carved out *before* this check runs: it's only ever "the current user's reply" in the sense that this account posted it, but it isn't a completion — it's an unimplemented fix spec. Once it has a `+1` from the current user, it's pulled out and addressed; until then, it correctly falls through to this same live check and stays excluded.
- The prompt template itself asks the backend to check for existing thread replies and skip re-fixing (but still acknowledge) anything that looks already addressed — an additional, backend-side layer of dedup on top of the code-level filters above. The template is explicit that "already addressed" means a reply pointing at a real, already-landed change (e.g. an "Addressed in `<commit-url>`" reply), not merely a reply that *describes* a proposed fix in detail — a distinction the code-level focused-review selection above no longer leaves to the backend to get right for marker replies specifically, but which still matters for ordinary human/bot comments.

Because none of this is state-file-based, resolving a thread on GitHub (or having the account reply, or making the account approve nothing relevant here) is what "marks it done" — there's no way to force a re-run of an already-answered comment other than manually reopening/unresolving the thread or removing the account's reply.

## Notes

- **Review-level comments have no reply-based dedup in code.** The `our_login`-based filter only checks inline-comment thread replies and issue-comment authorship — a PR-level review comment (`type: review`) is refetched and resent to the backend on every run for as long as it exists, unless the backend decides (per the template's Step 0) that it's noise and does nothing. A real review comment with no reply from the account will keep being retried indefinitely.
- **Push happens per comment, not per PR.** The runner pushes immediately after each comment's backend invocation finishes successfully, so progress is saved incrementally. If a push fails partway through a PR's comment list, earlier commits for that PR are already on `origin`, but the remaining comments in that PR are skipped for this run (the comment loop breaks) rather than retried later in the same invocation.
- Push is a plain `git push origin <branch>` — never force-pushed.
- Bot filtering only applies to issue/timeline comments, and it happens in two places with slightly different rules: the comment-fetch step drops any issue comment whose author ends in `[bot]` or whose body contains a `[` character followed by `bot]` (case-insensitive); the pending-feedback check's own issue-comment check separately hardcodes excluding `github-actions[bot]` and `dependabot[bot]`. Inline and review-level comments are never bot-filtered by the runner itself — that judgment is left entirely to the backend via the template's Step 0.
- PRs you authored (`gh pr list --author @me`) and PRs assigned to you (`gh pr list --assignee @me`) are both considered — the two lists are merged and deduplicated by PR number. A PR you're merely a *requested reviewer* on (not assigned) is still never touched by this command; that's `review-requested`'s job.
- Draft PRs are always skipped, unconditionally — there's no config flag to include them.
- A backend exception for one comment (e.g. a timeout) is logged and only skips that comment — it doesn't stop the rest of the PR's comment list or the rest of the batch.
- Subject to the shared locking, `gh` account, and working-directory-mutation caveats in
  [Shared behavior](index.md#shared-behavior).
- **Focused-review-bot replies are always gated behind a `+1` reaction, and are addressed as themselves, not as a proxy for the original comment.** Normally, any inline thread whose last reply was posted by this account is skipped as "already answered" — that's exactly backwards for a `[focused-review-bot]` marker reply, which is a *proposed* fix, not a completed one. So instead: an inline thread whose last reply carries the marker is only added to the actionable list once the current user has left a `+1` on that specific reply — checked live via the GitHub reactions API on every run, with no additional state file (if the reactions lookup itself fails, the thread is treated as not-yet-approved — fails closed). Once approved, the thread is addressed using the *marker reply's own* body/author/comment-ID, not the terse original finding (SonarQube, vibe-heal, etc.) that triggered `focused-review` in the first place; the original finding is still passed to the backend, but only as labeled background context. The backend is also told to skip its own act/reply-only/skip triage for these — the `+1` already made that call.
