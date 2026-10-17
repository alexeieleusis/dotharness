# ci-fix

`ci-fix` fixes CI overnight on your own open pull requests. For each PR you
authored whose required checks have failed, it reproduces the failure locally,
makes the smallest fix, and pushes it to the PR's own branch, with the goal of
making CI green. It is off until you set `[ci_fix].enabled = true`
(`enabled = false` by default).

It never approves or merges a PR. Approving belongs to
[`auto-approve`](auto-approve.md).

## Usage
```
harness run [--config PATH] [--verbose] ci-fix [--dry-run] [--pr N]
```

- `--config PATH` — Path to the `.harness.toml` config file. Defaults to
  `./.harness.toml`. It belongs to the `run` group, so it must precede
  `ci-fix`.
- `--verbose` — Enables DEBUG-level logging and mirrors log output to stdout.
  Logs always go to `~/.local/share/dotharness/logs/ci-fix/<date>.log`.
- `--dry-run` — Does every read and runs the commands locally, but makes no
  commit and no push. Attempt counters are unchanged.
- `--pr N` — Processes only PR number `N`. This is how Sisyphus calls it.

When `[ci_fix].enabled` is `false` (the default), the command does nothing and
exits 0. It does not even take the lock.

## What it does

1. Takes the shared working-directory lock (see
   [Shared behavior](index.md#shared-behavior)). A second concurrent run exits
   immediately.
2. Lists the PRs in scope: open, not a draft, authored by the current `gh`
   account, in this repository.
3. Reads each PR's check status with `gh pr checks`.
4. For a failed required check, reads the failing job's log with
   `gh run view --log-failed`.
5. Finds the command that job ran in the workflow file under
   `.github/workflows/`. Step names such as `Run tests` repeat across
   workflows, so only the workflow the failing check belongs to is searched.
6. Fetches `origin` and checks out the PR branch with
   `git_fetch_and_checkout`, in a dedicated clone or worktree.
7. Runs that command locally.
8. Makes the smallest fix, with the agent instructed by the fix prompt file
   (see [The fix prompt](#the-fix-prompt)).
9. Runs the command again.
10. Pushes to the PR's own branch only if the command passes locally.
11. Restores state with `git_restore`.

The target is "make it green". There is no fixed table mapping CI jobs to
commands; the command comes from the workflow file each time.

## Bounds

- At most `max_fix_attempts` agent runs per PR (default 3), whether or not the
  fix then passes or is pushed. The count persists across runs, so re-running
  does not reset it.
- A failure that does not reproduce locally, or that still fails once the cap
  is reached, becomes a digest entry. Nothing more is pushed for it.
- Pushes are plain pushes to the PR's own branch. There is no rebase and no
  force-push.

## Config

Settings live in the `[ci_fix]` section of `.harness.toml` (see
[Configuration](../configuration.md)). Unknown keys are rejected.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Turns the command on. |
| `max_fix_attempts` | `3` | Maximum agent runs per PR, counted across runs. |

## Safety conditions

- Work happens in a dedicated clone or worktree, never your everyday
  checkout.
- State is always restored, including after an error.
- It never approves or merges a PR.
- It skips PRs not authored by the acting user, draft PRs and closed PRs.
- It pushes only after the command passes locally, and only to the PR's own
  branch.
- A digest entry is recorded for exactly these cases: the failure did not
  reproduce, the attempt cap was reached, the check output could not be
  parsed, a rebase is needed, or the check is still failing after a fix.

Attempt counters and digest entries are written by the runner; this page only
describes them.

## The fix prompt

The fixing agent is instructed by `knowledge/pr-review/ci-fix-prompt.md`. The
runner uses that file as-is; change the file to change how the agent works.

## Known limitation

`gh pr checks` can exit with code 8 while still printing JSON. That
combination is not verified against live GitHub (PR #96). The command
reads the output the way `auto-approve` does: exit code 8 with empty output
means the status is unknown; otherwise the JSON is parsed, and output that is
not a list of check objects also means unknown. If parsing fails, the PR gets
a digest entry instead of a fix attempt.

## Errors

If any PR fails (a read failed, a git error, a push failed), the command logs
it, keeps going with the other PRs, and exits non-zero at the end with the
list of failures.
