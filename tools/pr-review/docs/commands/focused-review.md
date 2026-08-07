# focused-review

`focused-review` elaborates SonarQube-posted review comments that cite a knowledge file from the `jpablo/vibe-types` catalog (via a `knowledgeUrl()`-generated link embedded in the ESLint rule message that fed the SonarQube finding) into a detailed, actionable refactor description, posted as a reply on the same comment thread. It sits between `review-prs` (which posts the terse SonarQube findings in the first place) and the AI-authored reviews (`self-review`, `review-requested`) in the `all` sequence — it doesn't produce a review or fix code, it only enriches an existing automated comment with grounded, specific guidance.

## Usage
```
harness run [--config PATH] [--verbose] focused-review
```
- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml` (resolved relative to the current directory) if omitted. This option belongs to the `run` group, so it must precede `focused-review` on the command line.
- `--verbose` — enables DEBUG-level logging and also mirrors log output to stdout even when stdout isn't a TTY. Logs are always written to `~/.local/share/dotharness/logs/focused-review/<date>.log`.

## What it does

1. Acquires an exclusive file lock keyed on `repo_slug`, shared with the other four commands; a second concurrent invocation for the same repo — running this or any other command — exits immediately with an error instead of waiting.
2. If `focused_review.enabled` is `false`, logs and exits — the whole command is a no-op. (Independent of `vibe_heal.enabled` — the two toggles are not linked.)
3. Resolves a GitHub token via `harness.gh_token_cmd` and builds a subprocess environment from `harness.path_prepend` / `harness.env` plus `GITHUB_TOKEN`.
4. Loads the prompt template `pr-review/focused-review.md` from `harness.knowledge_dir`, and constructs a `Backend` for `harness.backend` (`opencode` or `claude`), with `GITHUB_TOKEN` merged into its environment.
5. Lists open, non-draft PRs whose author matches `vibe_heal.authors` — the same eligibility `review-prs` uses (reused via a shared helper). If none, exits.
6. Detaches HEAD and records the current commit so the working tree can be restored after each PR.
7. For each eligible PR, in ascending order:
   - Fetches all comments via `scripts/pr-comments.py fetch --pr <N>` (same mechanism `address-comments` uses) without checking out the branch yet.
   - Filters to inline comments whose body contains a `https://raw.githubusercontent.com/jpablo/vibe-types/<commit>/<path>.md` URL and that don't already have a reply containing the `[focused-review-bot]` marker.
   - If none match, moves on to the next PR without checking out anything.
   - Otherwise, fetches and checks out the PR's head branch (using the shared rebase/reset behavior — see [Shared behavior](index.md#shared-behavior)), then for each matching comment:
     - Resolves the cited knowledge file's content by running `git show <commit>:<path>` inside `focused_review.vibe_types_repo`. If the commit isn't available locally, fetches it and retries once; if that still fails, fetches `origin/main` and tries `git show origin/main:<path>` instead. If all three attempts fail, logs a warning and skips this comment.
     - Builds a prompt from the `focused-review.md` template plus the comment's file/line/URL/body/diff-hunk and the resolved knowledge-file content.
     - Runs the configured backend against the checked-out working directory. Per the template's own instructions, the backend reads the flagged file in full, writes a refactor description sized for a PR comment, and posts it itself as a reply via `gh api .../pulls/<N>/comments/<ID>/replies`, appending the `[focused-review-bot]` marker. The backend must not edit code, commit, or push for this command.
     - If the backend invocation raises, the error is logged and the loop moves to the next matching comment.
   - Any other exception while processing the PR is caught and logged; the run moves on to the next PR regardless.
   - The working tree is always restored to the SHA recorded in step 6 before moving to the next PR.
8. After all PRs, the working directory is left checked out (detached) at `origin/main`.

## Configuration

Only these `.harness.toml` fields affect `focused-review`; see [`../configuration.md`](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `focused_review.enabled` | Master switch — command is a no-op unless `true` |
| `focused_review.vibe_types_repo` | Local git checkout of `jpablo/vibe-types` used to resolve knowledge-file content via `git show` |
| `vibe_heal.authors` | Reused as-is to decide which PRs are eligible (same set `review-prs` uses) |
| `harness.backend` / `harness.backend_timeout_seconds` | Which AI backend writes each refactor description, and its timeout |
| `harness.gh_token_cmd` | Command used to fetch the GitHub token exported as `GITHUB_TOKEN` |
| `harness.knowledge_dir` | Must contain `pr-review/focused-review.md`, the prompt template for this runner |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries / env vars for git, `gh`, and the backend subprocess |
| `repo.name` | GitHub repo (`org/repo`) queried via `gh`, and the basis of the lock key (`repo_slug`) |
| `repo.working_dir` | Local git checkout that gets detached, fetched, and checked out branch-by-branch |

Fields this runner does **not** read: `vibe_heal.enabled`, `vibe_heal.python`, `vibe_heal.vibe_heal_timeout`, `vibe_heal.vibe_heal_post_timeout`, `repo.subdir[]` (any of its fields), `repo.opencode_dir`, `harness.review_knowledge_file` — none of those affect `focused-review`.

## State and idempotency

`focused-review` does not persist state between runs — like `address-comments`, there is no "last processed PR/comment" file, and `harness state reset focused-review` is not supported for this command.

Instead, it avoids reprocessing using a live signal read from GitHub on each run: a matching comment is skipped if any existing reply on its thread already contains the literal `[focused-review-bot]` marker. This is deliberately not an author-based check — the original SonarQube comment, a focused-review reply, and a later `address-comments` "addressed in ..." reply can all be posted under the same `gh` identity, so "last reply author" cannot distinguish them.

## Notes

- Only **inline** review comments are scanned. The rare top-level fallback comment `vibe_heal` posts when GitHub rejects an inline review (too many findings, lines outside the diff) is not handled by this command.
- The recognized knowledge-source repo slug (`jpablo/vibe-types`) is currently hardcoded, not configurable.
- Draft PRs are always skipped, unconditionally.
- A backend exception for one comment is logged and only skips that comment — it doesn't stop the rest of the PR's matching comments or the rest of the batch.
- Subject to the shared locking, `gh` account, and working-directory-mutation caveats in [Shared behavior](index.md#shared-behavior).
