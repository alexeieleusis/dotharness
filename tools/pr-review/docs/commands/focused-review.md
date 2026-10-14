# focused-review

`focused-review` turns the review comments that SonarQube posts into detailed, actionable refactor descriptions. It handles only the comments that cite a knowledge file from the `jpablo/vibe-types` catalog. Those comments cite it through a `knowledgeUrl()`-generated link embedded in the ESLint rule message that fed the SonarQube finding. For each such comment, the command has the backend either post a refactor description as a reply on the same comment thread, or, when the backend judges the finding a false positive (whatever the cause), resolve the review thread and minimise the root comment, with no text posted. The refactor-description path is unchanged.

It sits between `review-prs` (which posts the terse SonarQube findings in the first place) and the AI-authored reviews (`self-review`, `review-requested`) in the `all` sequence. It does not produce a review or fix code. It only enriches an existing automated comment with grounded, specific guidance, or quietly dismisses it when the finding is a false positive. It never deletes a comment.

## Usage
```
harness run [--config PATH] [--verbose] focused-review
```
- `--config PATH` — path to the `.harness.toml` config file. If omitted, it defaults to `./.harness.toml` (resolved relative to the current directory). The option belongs to the `run` group, so it must precede `focused-review` on the command line.
- `--verbose` — enables DEBUG-level logging. It also mirrors log output to stdout, even when stdout is not a TTY. The command always writes logs to `~/.local/share/dotharness/logs/focused-review/<date>.log`.

## What it does

1. The command acquires an exclusive file lock, keyed on the resolved `repo.working_dir` path (not `repo.name`/`repo_slug` — see [Shared behavior](index.md#shared-behavior)). The lock is shared with the other four commands. A second concurrent invocation against the same working directory — running this or any other command — exits immediately with an error instead of waiting.
2. If `focused_review.enabled` is `false`, the command logs and exits. The whole command is a no-op. The two toggles are not linked: `focused_review.enabled` is independent of `vibe_heal.enabled`.
3. The command resolves a GitHub token via `harness.gh_token_cmd`. It then builds a subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
4. The command loads the prompt template `pr-review/focused-review.md` from `harness.knowledge_dir`. It also constructs a `Backend` for `harness.backend` (`opencode` or `claude`). It merges `GITHUB_TOKEN` into the backend's environment.
5. The command lists the open PRs, drafts included. It keeps a PR when the currently active `gh` account (`@me`) is the author, assignee, or has a review requested (the union of the three). It deduplicates by PR number and sorts the result ascending. The listing uses `list_open_prs_for_current_user`, which does **not** read `vibe_heal.authors`. `review-prs` uses `list_open_prs_matching_authors` against the configured author list instead. If no PR is eligible, the command exits.
6. The command detaches HEAD and records the current commit so it can restore the working tree after each PR.
7. For each eligible PR, in ascending order:
   - It fetches all comments via `scripts/pr-comments.py fetch --pr <N>` (the same mechanism `address-comments` uses), without checking out the branch yet.
   - It keeps only the inline comments whose body contains a `https://raw.githubusercontent.com/jpablo/vibe-types/<commit>/<path>.md` URL, and skips the ones that already have a reply containing the `[focused-review-bot]` marker, or whose thread is resolved or whose root comment is minimised (see the thread-state fetch below and [State and idempotency](#state-and-idempotency)).
   - If none match, it moves on to the next PR without checking out anything.
   - It then fetches the PR's review-thread state through the GraphQL `reviewThreads` query: the thread id, `isResolved`, and for each comment its node id, `databaseId` and `isMinimized`. It merges this into the comments fetched by `scripts/pr-comments.py fetch --pr <N>`, matching by `databaseId`. The resolved-thread and minimised-comment skip applies to the merged data, so a resolved thread is skipped whoever resolved it. If the thread-state fetch fails, the command logs a warning and skips that PR for the run, then moves on to the next PR. No checkout is done, because the fetch precedes the checkout.
   - Otherwise, it fetches and checks out the PR's head branch (using the shared rebase/reset behavior — see [Shared behavior](index.md#shared-behavior)), then for each matching comment:
     - It resolves the cited knowledge file's content by running `git show <commit>:<path>` inside `focused_review.vibe_types_repo`. If the commit is not available locally, it fetches the commit and retries once. If that still fails, it fetches `origin/main` and tries `git show origin/main:<path>` instead. If all three attempts fail, it logs a warning and skips this comment.
     - It builds a prompt from the `focused-review.md` template, the comment's file/line/URL/body/diff-hunk, and the resolved knowledge-file content. The prompt also carries the thread node id and the comment node id from the thread-state fetch, so the backend can act on them.
     - It runs the configured backend against the checked-out working directory. Per the template's own instructions, the backend reads the flagged file in full. It then produces one of two outcomes:
       - Real finding: it writes a refactor description sized for a PR comment and posts it itself as a reply via `gh api .../pulls/<N>/comments/<ID>/replies`, appending the `[focused-review-bot]` marker.
       - False positive (whatever the cause): it resolves the thread (GraphQL `resolveReviewThread`), then minimises the root comment (`minimizeComment`, classifier `RESOLVED`). It posts no text and no marker.

       The backend must not edit code, commit, or push for this command.
     - If the backend invocation raises, the command logs the error and moves to the next matching comment.
   - The command catches and logs any other exception while processing the PR. It then moves to the next PR regardless.
   - It always restores the working tree to the SHA recorded in step 6 before moving to the next PR.
8. After all PRs, the command leaves the working directory checked out (detached) at `origin/main`.

## Configuration

Only these `.harness.toml` fields affect `focused-review`. See [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `focused_review.enabled` | Master switch. The command is a no-op unless the field is `true` |
| `focused_review.vibe_types_repo` | Local git checkout of `jpablo/vibe-types` used to resolve knowledge-file content via `git show` |
| `harness.backend` / `harness.backend_timeout_seconds` | Which AI backend writes each refactor description, and its timeout |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/focused-review.md`, the prompt template for this runner |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for git, `gh`, and the backend subprocess |
| `repo.name` | GitHub repo (`org/repo`) queried via `gh` |
| `repo.working_dir` | The local git checkout that is detached, fetched, and checked out branch-by-branch. Its resolved path is also the basis of the lock key (see [Shared behavior](index.md#shared-behavior)) |

Fields this runner does **not** read: `vibe_heal.authors`, `vibe_heal.enabled`, `vibe_heal.python`, `vibe_heal.vibe_heal_timeout`, `vibe_heal.vibe_heal_post_timeout`, `repo.subdir[]` (any of its fields), `repo.opencode_dir`, and `harness.review_knowledge_file`. None of those fields affect `focused-review`.

## State and idempotency

`focused-review` does not persist state between runs. Like `address-comments`, it has no "last processed PR/comment" file. The command does not support `harness state reset focused-review`.

Instead, it avoids reprocessing with two live signals. It reads both from GitHub on each run:

1. Refactor-description path: it skips a matching comment if any existing reply on its thread already contains the literal `[focused-review-bot]` marker.
2. False-positive path: it skips a matching comment whose review thread is resolved, or whose root comment is minimised. A false positive leaves no marker reply, so the GitHub thread and comment state is the signal.

This check is deliberately not author-based. The original SonarQube comment, a focused-review reply, and a later `address-comments` "addressed in ..." reply can all be posted under the same `gh` identity. So a check on the last reply author cannot distinguish them.

## Notes

- The command scans only **inline** review comments. It does not handle the rare top-level fallback comment that `vibe_heal` posts when GitHub rejects an inline review (too many findings, lines outside the diff).
- The recognized knowledge-source repo slug (`jpablo/vibe-types`) is currently hardcoded, not configurable.
- Draft PRs are listed like any other PR. There's no draft-specific behavior and no config flag.
- If a backend exception occurs for one comment, the command logs it and skips only that comment. It does not stop the rest of the PR's matching comments or the rest of the batch.
- Resolve and minimise are best effort. If either fails, the command logs it and ignores it. It does not stop the remaining comments or PRs.
- The real `resolveReviewThread` and `minimizeComment` calls with the harness token have not yet been verified against GitHub. That manual check is pending.
- This command is subject to the shared locking, `gh` account, and working-directory-mutation caveats in [Shared behavior](index.md#shared-behavior).
