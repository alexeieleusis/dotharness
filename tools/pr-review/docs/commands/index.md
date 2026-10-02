# Commands

Each command below is a subcommand of `harness run`. Each command loads a
[`.harness.toml`](../configuration.md) config file (`./.harness.toml` by default, or
the path passed to `--config`) before doing anything else.

```
harness run [--config PATH] [--verbose] <command> [command options]
```

| Command | Scope | Purpose |
|---|---|---|
| [`review-prs`](review-prs.md) | Every open, non-draft PR in the repo | Sweep all open PRs and post `vibe_heal`/SonarQube-style static-analysis feedback, tracking progress with a persisted watermark. |
| [`focused-review`](focused-review.md) | Open, non-draft PRs where the current `gh` user is author, assignee, or requested reviewer | Expand SonarQube comments into a detailed refactor description, citing a `jpablo/vibe-types` knowledge file. Post the description as a reply. |
| [`review-requested`](review-requested.md) | PRs where review was explicitly requested from the `gh` account | Have the configured AI backend produce inline + summary code review comments. The command reacts to GitHub review-request state, not a schedule. |
| [`self-review`](self-review.md) | Your own open PRs (`--author @me`) | Get an automated first-pass AI review of your own PRs before asking a human. |
| [`address-comments`](address-comments.md) | Open PRs you authored or are assigned to, with pending reviewer feedback | Have the AI backend read unresolved review comments, make the smallest fix (or reply), commit, and push. |
| [`local-review`](local-review.md) | The branch checked out in `repo.working_dir`, compared with its base ref | Review a local branch before you push, and write findings to Markdown files. No GitHub access. `harness run all` does not include `local-review`. |

There is also an optional `regret-review` pass — enabled via the [`[regret_review]`](../configuration.md#regret_review) config section — that re-checks old, ignored review comments on the PRs `self-review` and `review-requested` already cover.

There's also a convenience command that runs all five in sequence:

```
harness run [--config PATH] [--verbose] all
```

`all` runs the five commands in this order: `review-prs`, `focused-review`,
`self-review`, `review-requested`, `address-comments`. `harness run all` does not include `local-review`. If one command fails, `all`
continues to the next. If any command fails, `all` exits non-zero.

## Shared behavior

- **Locking.**
  - Every command acquires a non-blocking file lock before doing any work.
  - The lock key is the resolved `repo.working_dir` path, not `repo.name` or `repo_slug`.
  - All five commands share the lock, not just same-command invocations. Since all five commands mutate the same `repo.working_dir` checkout, a second concurrent invocation against the same working directory exits immediately. It does not queue or race the first invocation.
  - Two configs that point at the same checkout share a lock even if their `repo.name` differs. Two configs with the same `repo.name` but separate `working_dir` clones (e.g. a second clone dedicated to a cheaper backend) get independent locks and can run concurrently.
- **Logging.** Logs always go to `~/.local/share/dotharness/logs/<command>/<date>.log`.
  `--verbose` additionally enables DEBUG-level logging and mirrors it to stdout.
- **Working directory mutation.** Commands that talk to an AI backend or `vibe_heal`
  check out PR branches directly inside `repo.working_dir`. Each checkout runs
  `git checkout -B <branch> origin/<branch>`, which unconditionally repoints the local
  branch to its remote tracking branch and discards any local-only commits. The
  command restores the original detached HEAD commit afterward. Don't point
  `working_dir` at a checkout with uncommitted work.
- **`gh` account state.** Several commands rely on `gh`'s currently active authenticated
  account (for `--author @me`, `--assignee @me`, `user-review-requested:@me`, and posting as "you"). If you
  juggle multiple `gh` accounts, the active one is global machine state, not scoped to
  a particular `.harness.toml`.

## Related commands

- `harness init [DIRECTORY]` — scaffold a starting `.harness.toml`.
- `harness validate [DIRECTORY] [--config PATH]` — sanity-check a config file.
- `harness state reset <command> [--config PATH] [--yes]` — clear persisted state for
  `review-prs` or `self-review` (see each command's own "State and idempotency" section),
  or delete the review output of `local-review` (see [`local-review`](local-review.md#re-runs)).
- `harness schedule install <command> --every <duration> [--config PATH] [--scheduler cron|launchd]` —
  install a recurring `cron`/`launchd` schedule for a command.
- `harness schedule uninstall <command> [--config PATH] [--scheduler cron|launchd]` — remove one.
- `harness schedule list` — list installed schedules.

See [Configuration](../configuration.md) for the full `.harness.toml` schema referenced
throughout these pages.
