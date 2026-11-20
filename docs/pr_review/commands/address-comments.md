# address-comments

`address-comments` scans open pull requests you authored or are assigned to for reviewer feedback: unresolved inline review threads, review-level comments, non-bot issue comments, and bot-authored PR-level comments you have marked with a rocket reaction (see [Rocket path](#rocket-path)). For each, the configured AI backend decides whether it warrants a change, makes the smallest fix if so, commits, and replies on GitHub.

Unlike `self-review` or `review-requested`, which *produce* review comments, `address-comments` *consumes* them. It's the runner you schedule (or run by hand) to keep PRs moving after humans (or bots) have left feedback on them.

## Usage
```
pr_review run [--config PATH] [--verbose] address-comments
```

The CLI name is `pr_review`. The config file names (`.harness*.toml`) keep the old prefix on purpose.

- `--config PATH` — path to the `.harness.toml` config file. The default is `./.harness.toml`, resolved relative to the current directory. This option belongs to the `run` group, so it must precede `address-comments` on the command line.
- `--verbose` — enables DEBUG-level logging and mirrors log output to stdout even when stdout isn't a TTY. Logs are always written to `~/.local/share/dotharness/logs/address-comments/<date>.log`.

## What it does

1. Acquires an exclusive file lock keyed on the resolved `repo.working_dir` path (not `repo.name`/`repo_slug` — see [Shared behavior](index.md#shared-behavior)). The lock is shared with the eight other commands that take it (`review-prs`, `ci-fix`, `focused-review`, `self-review`, `review-requested`, `merge`, `auto-approve` and `local-review`). `focused-review` additionally takes a second, separate lock keyed on the `vibe-types` working directory. A second concurrent invocation against the same working directory — running this or any other command — exits immediately with an error instead of waiting.
2. Resolves the host token via `harness.host_token_cmd` (else the selected host's default, `gh auth token` for the reference host). It builds the subprocess environment from `harness.path_prepend` / `harness.env` plus the host's credential variables (`GITHUB_TOKEN` and `GH_TOKEN` for the reference host).
3. Lists the open PRs to check: two code-host listings of open PRs, one with the current user's login as author and one as assignee (the login replaces `@me`; each is limited to 1000). An empty login lists nothing. The results are merged and deduplicated by PR number, then sorted ascending.
4. Loads the single prompt template `pr-review/address-comment.md` from `harness.knowledge_dir`. It constructs a `Backend` for `harness.backend` (`opencode` or `claude`), with the host's credential variables (`GITHUB_TOKEN` and `GH_TOKEN` for the reference host) merged into its environment.
5. If `repo.opencode_dir` is set, it computes that path relative to `repo.working_dir` (`plugin_prefix`). That path is used later to restrict which inline comments are considered.
6. Detaches HEAD and records the current commit so it can restore the working tree after each PR.
7. For each PR, in ascending order:
   - Calls a GraphQL query for unresolved review threads. If any thread is unresolved, the PR has "pending feedback." Otherwise, it falls back to checking for any issue/timeline comment not authored by a bot (the author's `is_bot` flag is set, or the login is `github-actions[bot]` or `dependabot[bot]`), and then for a bot comment that is due under the rocket path. If none of these finds anything, the PR is skipped entirely — no checkout, no backend calls.
   - Resolves the current host user's login once, before listing PRs, and reuses it for the rest of the run.
   - Once the PR is selected (it passed the pending-feedback check above), checks whether the current user is currently a requested reviewer on this PR (`preserve_reviewer_request` wraps the rest of the per-PR work). This happens before the fetch and checkout below.
   - Fetches and checks out the PR's head branch (using `git checkout -B` to `origin/<branch>` — see [Shared behavior](index.md#shared-behavior)).
   - Fetches all comments by running the host's comments script (`Discussion.comments_script()`, for the reference host `src/dotharness/code_host/scripts/pr-comments.py`) with `fetch --pr <N>` and reads the JSON it caches at `~/.harness/cache/pr-<N>-comments.json`. The actionable set is:
     - all inline (review-thread) comments,
     - all PR-level review comments, and
     - issue comments whose author doesn't end in `[bot]`.

     Bot-authored (or own-login) PR-level comments that carry a rocket from the current user are supplied separately from typed host comments (see [Rocket path](#rocket-path)) and added to the list below.
   - If a second GraphQL query (mapping unresolved threads to their comment IDs) succeeds, inline comments are further filtered to only those in a still-unresolved thread. If that query fails, this filter is skipped and inline comments are left as fetched.
   - Before the ordinary "already answered" check, it pulls out any inline comment whose most recent thread reply carries a `[focused-review-bot]` marker, provided the thread is unresolved and the root comment is not minimised. No reaction or other human approval is involved. If the live thread-state lookup fails, marked threads are skipped for that PR (fail closed) rather than risk addressing a resolved or minimised thread. This selection is programmatic (code-level), not left to the backend, because a detailed marker reply reads like a finished writeup, and an LLM asked to judge "has this been addressed?" can (and did) conclude it already had been. Each selected thread is re-pointed at that reply as the actual comment to address: the reply's body/author/ID become the comment's body/author/ID (so the backend's own reply threads correctly). File path, line, and diff context are inherited from the parent thread. See Notes below.
   - Drops the remaining comments that look already answered by this account. Those are inline comments whose most recent thread reply was authored by the current user, or issue comments authored by the current user. (Review-level comments have no such check — see Notes.) A marker reply is not subject to this check while it is the last reply (see the carve-out above). Once anything is posted after it, the thread falls through to this check and stays excluded.
   - If `repo.opencode_dir` is configured, it further restricts inline comments to those whose file path falls under that subdirectory. Review and issue comments are unaffected by this filter.
   - If nothing survives all the filtering, the PR is skipped.
   - Otherwise, for each remaining comment, in order:
     - Builds a prompt from the `address-comment.md` template, comment-specific details, the PR number, and the repo name. The comment-specific details are:
       - inline comments: file/line, comment ID, author, URL, body, diff context, and thread replies
       - review comments: ID/author/state/URL/body
       - issue comments: ID/author/URL/body
       For a selected focused-review comment, the prompt tells the backend to skip the template's Step 0 (act/reply-only/skip triage) entirely, because the programmatic selection already decided this needs a real fix. The prompt also includes the original automated finding as background-only context, explicitly labeled as not the thing to implement.
     - Runs the configured backend (see `harness.backend` in [Configuration](../configuration.md)) against the checked-out working directory (or `opencode_dir`, if configured). Per the template's own instructions, the backend decides for itself whether an ordinary comment needs a code change, a reply-only response, or nothing at all (e.g. "LGTM"-style noise is skipped outright). If it acts, it stages only the files it touched, commits with a message that links back to the comment, and posts the reply to GitHub itself — via the inline-reply API for inline comments, `gh api .../issues/<N>/comments` for issue comments, or `gh pr comment` for review comments. None of that (commit or GitHub reply) is code in this runner. It all happens inside the backend's own tool use.
     - If the backend invocation raises (e.g. a timeout after its internal retry), the error is logged and the loop moves to the next comment for this PR. Any fast-forward commit the backend made before failing is still pushed with the others at the end.
     - Nothing is pushed between comments. After the last comment for the PR, the runner itself runs `git push origin <branch>` once (see the push notes below). If that push fails, a warning is logged, it is not retried, and the per-comment "handled" state is not rolled back. Processing continues with the next PR.
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
| `harness.host_token_cmd` | Command used to fetch the host token, exported as `GITHUB_TOKEN` and `GH_TOKEN` (reference host). Absent means `gh auth token`. `harness.gh_token_cmd` was removed and fails the config load |
| `harness.knowledge_dir` | Must contain `pr-review/address-comment.md`, the prompt template for this runner |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for git, `gh`, and the backend subprocess |
| `repo.name` | GitHub repo (`org/repo`) queried through the code host |
| `repo.working_dir` | Local git checkout that gets detached, fetched, and checked out branch-by-branch. Its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |
| `repo.opencode_dir` | If set: passed to the backend as its `--dir`, and used to restrict inline comments to that subdirectory |
| `address_comments.enabled` | Turns the command on. `false` skips the run entirely (default `true`) |
| `address_comments.trusted_commenters` | Restricts which comment authors are considered at all, including a rocketed bot comment. `"*"` (default) considers everyone |

Fields this runner does **not** read: `harness.review_knowledge_file`, `repo.subdir[]` (any of its fields), and the entire `[vibe_heal]` section.

## State and idempotency

`address-comments` persists state in `address_comments.json`, in the same per-repo state directory as the other commands. It holds two maps per PR:
- `replied`: keys (`<type>:<id>`, for example `review:<id>` or `issue:issue-comment-<id>`) of PR-level comments already addressed by a clean run in which a commit or a reply was observed. Issue and review comments have no reply linkage in the GitHub API, so this is the only record that one was answered.
- `handled_versions`: for each rocket-signalled comment, the edit version (`updated_at`) last handled, the number of rounds run, and the creation time of the rocket those rounds belong to.

Entries for PRs that are no longer open are pruned at the start of each run (skipped if listing the open PRs fails). `pr_review state reset address-comments` currently fails with `No state file for command: address-comments`, because the command is not in the `_COMMAND_FILES` table in `state.py` (only `review-prs` and `self-review` are). To force a re-run, reopen or unresolve the thread, remove the account's reply, or edit the state file by hand.

Beyond that persisted state, it avoids reprocessing with live signals read from GitHub on each run:
- A PR is skipped up front unless it currently has an unresolved review thread, a non-bot issue comment, or a rocket-signalled comment that is due (the pending-feedback check).
- Inline comments are further narrowed to ones still in an unresolved thread. This is best-effort: if the GraphQL lookup fails, the narrowing is skipped, and previously-resolved inline comments are *not* excluded that run.
- A comment is treated as already handled if the current `gh` user's login is the author of the issue comment, or the author of the most recent reply in an inline comment's thread. This is a live check against GitHub, in addition to the `replied` record above. A `[focused-review-bot]` marker reply is an exception carved out *before* this check runs. It's only "the current user's reply" in the sense that this account posted it. It isn't a completion — it's an unimplemented fix spec. It is pulled out and addressed whenever it is the last reply in the thread. Once anything is posted after it (for example the backend's own reply), the thread stops re-triggering and is excluded by this same live check.
- The prompt template itself asks the backend to check for existing thread replies and skip re-fixing (but still acknowledge) anything that looks already addressed. This is an additional, backend-side layer of dedup on top of the code-level filters above. The template is explicit about what "already addressed" means: a reply pointing at a real, already-landed change (e.g. an "Addressed in `<commit-url>`" reply), not merely a reply that *describes* a proposed fix in detail. For marker replies specifically, the code-level focused-review selection above no longer leaves that distinction to the backend to get right. It still matters for ordinary human/bot comments.

Resolving a thread on GitHub (or having the account reply) marks inline comments done through the live checks, and a clean run marks issue and review comments done through `replied`. There's no supported command to force a re-run of an already-answered comment; see the manual options above.

### Rocket path

A rocket reaction from the current user's login on a PR-level (issue) comment makes `address-comments` address it, even when a bot wrote it (the ordinary path skips bot comments and the account's own comments). A comment is selected when our login has a `rocket` reaction on it and its `updated_at` is later than the stored handled version (or none is stored). Only bot comments and the account's own comments are checked for a rocket; a human comment keeps the ordinary rules and costs no reactions read.

- Each clean run stores the `updated_at` that was snapshotted before the backend ran. An edit to the comment after that, including one made during the run, is addressed in the next run, and so on after each edit while the rocket stays. Removing the rocket stops it.
- At most `MAX_ROUNDS_PER_ROCKET` (10) clean runs are made per rocket. At the cap, the run logs one warning naming the PR and comment and telling you to remove and re-add the rocket, which resets the count. See [`[address_comments]`](../configuration.md#address_comments).
- `trusted_commenters` still applies to the comment's author. A rocket does not override it.
- The prompt skips the template's Step 0 triage for these comments, like focused-review ones, and asks the backend to say in its reply which findings it did and did not address, and why.
- Fail closed: if the host reports no `updated_at`, or the reactions read fails, the comment is dropped with a warning. A host without a reactions read behaves as if no comment had a rocket (policy `degrade`).

## Notes

- **Review-level comments have no reply-based dedup in code.** The `our_login`-based filter only checks inline-comment thread replies and issue-comment authorship. A PR-level review comment (`type: review`) is deduplicated only by the persisted `replied` key, recorded after a clean run in which a commit or reply was observed. Until then it is refetched and resent to the backend on every run, unless the backend decides (per the template's Step 0) that it's noise and does nothing. A real review comment that the backend never acts on or replies to will keep being retried.
- **Push happens once per PR, after the last comment.** The runner pushes after all of a PR's comments have been handled, so commits for earlier comments are still local while later comments are processed. The push is skipped when nothing is ahead of `origin/<branch>` (a run with no commits pushes nothing) and when history was rewritten (HEAD moved to a commit that is not a descendant of the previous HEAD). A failed push is not retried; the unpushed local commits are kept under `refs/harness-recovery/<branch>-<sha12>` so a human can recover them by rebasing. Cancellation skips the push; the same recovery ref preserves the commits.
- **Commit links in backend replies resolve late.** Links of the form `https://github.com/<REPO>/commit/<HASH>` work only after the end push succeeds, and never if it fails.
- Push is a plain `git push origin <branch>` — never force-pushed.
- Bot filtering only applies to issue/timeline comments. It happens in two places with slightly different rules:
  - The comment-fetch step (`fetch_pr_comments`) drops any issue comment whose author ends in `[bot]`.
  - The pending-feedback check's issue-comment check uses `_is_bot_comment`: a comment is a bot comment if its author's `is_bot` flag is set, or its login is `github-actions[bot]` or `dependabot[bot]`.

  A rocket from the current user on a bot comment bypasses both (see [Rocket path](#rocket-path)).

  Inline and review-level comments are never bot-filtered by the runner itself. That judgment is left entirely to the backend via the template's Step 0.
- PRs you authored (listed by author) and PRs assigned to you (listed by assignee) are both considered — the two lists are merged and deduplicated by PR number. A PR you're merely a *requested reviewer* on (not assigned) is still never touched by this command. That's `review-requested`'s job.
- Draft PRs are processed exactly like non-draft PRs. There's no draft-specific behavior and no config flag.
- A backend exception for one comment (e.g. a timeout) is logged and only skips that comment. It doesn't stop the rest of the PR's comment list or the rest of the batch.
- Subject to the shared locking, `gh` account, and working-directory-mutation caveats in [Shared behavior](index.md#shared-behavior).
- **Focused-review-bot replies are addressed as themselves, not as a proxy for the original comment, and there is no human approval gate.** Normally, any inline thread whose last reply was posted by this account is skipped as "already answered." That's exactly backwards for a `[focused-review-bot]` marker reply, which is a *proposed* fix, not a completed one. Instead, an inline thread whose last reply carries the marker is added to the actionable list as long as the thread is unresolved and its root comment is not minimised. No reaction is needed. If the thread-state lookup fails, marked threads are skipped (fails closed). The thread is addressed using the *marker reply's own* body/author/comment-ID, not the terse original finding (SonarQube, vibe-heal, etc.) that triggered `focused-review` in the first place. The original finding is still passed to the backend, but only as labeled background context. The backend is also told to skip its own act/reply-only/skip triage for these. It should make the fix unless the spec says the finding is a false positive. In that case it makes no change and replies saying so. It must not reply with only an acknowledgment, and must not claim a fix it did not make. Because there is no human gate, the marker reply itself must have been authored by the current `gh` account (selection fails closed if that login is unknown) and by an author allowed by `[address_comments].trusted_commenters`. A marker pasted by anyone else is ignored. There is no config flag to disable this.
