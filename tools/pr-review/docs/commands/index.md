# Commands

Every command below is a subcommand of `harness run`, and every one of them loads a
[`.harness.toml`](../configuration.md) config file (`./.harness.toml` by default, or
the path passed to `--config`) before doing anything else.

```
harness run [--config PATH] [--verbose] <command> [command options]
```

| Command | Scope | Purpose |
|---|---|---|
| [`review-prs`](review-prs.md) | Every open, non-draft PR in the repo | Sweep all open PRs and post `vibe_heal`/SonarQube-style static-analysis feedback, tracking progress with a persisted watermark. |
| [`focused-review`](focused-review.md) | Every open, non-draft PR matching `vibe_heal.authors` | Elaborate SonarQube comments citing a `jpablo/vibe-types` knowledge file into a detailed refactor description, posted as a reply. |
| [`review-requested`](review-requested.md) | PRs where review was explicitly requested from the `gh` account | Have the configured AI backend produce inline + summary code review comments, reacting to GitHub review-request state rather than a schedule. |
| [`self-review`](self-review.md) | Your own open PRs (`--author @me`) | Get an automated first-pass AI review of your own PRs before asking a human. |
| [`address-comments`](address-comments.md) | Open PRs you authored or are assigned to, with pending reviewer feedback | Have the AI backend read unresolved review comments, make the smallest fix (or reply), commit, and push. |

There's also a convenience command that runs all five in sequence:

```
harness run [--config PATH] [--verbose] all
```

`all` runs `review-prs`, `focused-review`, `self-review`, `review-requested`, then
`address-comments`, continuing to the next runner even if one fails, and exits non-zero
if any of them failed.

## Shared behavior

- **Locking.** Every command acquires a non-blocking per-repo file lock (`<repo_slug>`)
  before doing any work. The lock is shared across all five commands, not just
  same-command invocations — since they all mutate the same `repo.working_dir` checkout,
  a second concurrent invocation for the same repo, running any of the five commands,
  exits immediately instead of queuing or racing the first.
- **Logging.** Logs always go to `~/.local/share/dotharness/logs/<command>/<date>.log`.
  `--verbose` additionally enables DEBUG-level logging and mirrors it to stdout.
- **Working directory mutation.** Commands that talk to an AI backend or `vibe_heal`
  check out PR branches directly inside `repo.working_dir`: each checkout rebases local
  commits onto `origin/<branch>`, falling back to a hard reset to `origin/<branch>` if
  that rebase conflicts (discarding local-only commits), and the original commit is
  restored afterward. Don't point `working_dir` at a checkout you have uncommitted work in.
- **`gh` account state.** Several commands rely on `gh`'s currently active authenticated
  account (for `--author @me`, `--assignee @me`, `user-review-requested:@me`, and posting as "you"). If you
  juggle multiple `gh` accounts, the active one is global machine state, not scoped to
  a particular `.harness.toml`.

## Related commands

- `harness init [DIRECTORY]` — scaffold a starting `.harness.toml`.
- `harness validate [DIRECTORY] [--config PATH]` — sanity-check a config file.
- `harness state reset <command> [--config PATH] [--yes]` — clear persisted state for
  `review-prs` or `self-review` (the only two commands with persisted state — see each
  command's own "State and idempotency" section).
- `harness schedule install <command> --every <duration> [--config PATH] [--scheduler cron|launchd]` —
  install a recurring `cron`/`launchd` schedule for a command.
- `harness schedule uninstall <command> [--config PATH] [--scheduler cron|launchd]` — remove one.
- `harness schedule list` — list installed schedules.

See [Configuration](../configuration.md) for the full `.harness.toml` schema referenced
throughout these pages.
