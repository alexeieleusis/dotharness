# ci-fix

`ci-fix` fixes CI overnight on your own open pull requests. For each PR you
authored whose required checks have failed, it reproduces the failure locally.
It makes the smallest fix. It then pushes the fix to the PR's own branch to
make CI green.
It is off until you set `[ci_fix].enabled = true`
(`enabled = false` by default).

It never approves or merges a PR. Approving belongs to
[`auto-approve`](auto-approve.md).

## Usage
```
pr_review run [--config PATH] [--verbose] ci-fix [--dry-run] [--pr N]
```

The CLI name is `pr_review`. The config file names (`.harness*.toml`) keep the old prefix on purpose.


- `--config PATH` — Path to the `.harness.toml` config file. Defaults to
  `./.harness.toml`. It belongs to the `run` group, so it must precede
  `ci-fix`.
- `--verbose` — Enables DEBUG-level logging. Mirrors log output to stdout.
  Logs always go to `~/.local/share/dotharness/logs/ci-fix/<date>.log`.
- `--dry-run` — Does every read and runs the commands locally. Makes no commit
  and no push. Attempt counters are unchanged.
- `--pr N` — Processes only PR number `N`. This is how Sisyphus calls it.

When `[ci_fix].enabled` is `false` (the default), the command does nothing and
exits 0. It does not even take the lock.

## What it does

1. Takes the shared working-directory lock (see
   [Shared behavior](index.md#shared-behavior)). A second concurrent run exits
   immediately.
2. Lists the PRs in scope: open, not a draft, authored by the current `gh`
   account, in this repository.
3. Reads each PR's check status with `gh pr checks`. Only the latest run of each check counts, as in [auto-approve](auto-approve.md#safety-conditions). So it ignores a failure that a rerun has already fixed. The stack-root rule of auto-approve does not apply here. For a stacked PR whose base has no required checks, every failing check is a candidate. This includes checks that are not required at the stack root. ci-fix runs only after the safety check blocks the PR. So it can only fix more, never approve.
4. For a failed required check, reads the failing job's log with
   `gh run view --log-failed`.
5. Finds the command that job ran in the workflow file under
   `.github/workflows/`. Step names such as `Run tests` repeat across
   workflows, so it searches only the workflow the failing check belongs to.
6. Fetches `origin` and checks out the PR branch with
   `git_fetch_and_checkout`, in a dedicated clone or worktree.
7. Runs that command locally.
8. Makes the smallest fix. The fixing agent is instructed by the fix prompt
   file (see [The fix prompt](#the-fix-prompt)).
9. Runs the command again.
10. Pushes to the PR's own branch only if the command passes locally.
11. Restores state with `git_restore`.

The target is "make it green". There is no fixed table mapping CI jobs to
commands. The command comes from the workflow file each time.

## Bounds

- At most `max_fix_attempts` counted agent runs per PR (default 3). A run
  counts when it changes the branch, or crashes or times out. A branch change
  is a new commit or any change to the working tree. It counts whether or not
  the fix then passes or is pushed. A run that leaves HEAD and the working
  tree exactly as it found them does not count. The count persists across runs
  until ci-fix finds the PR's CI green. That resets the count, so a later
  unrelated failure gets a full budget again. Dry runs never reset it.
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
| `max_fix_attempts` | `3` | Maximum counted agent runs per PR (runs that changed the branch or crashed), counted across runs. |

## Safety conditions

- Work happens in a dedicated clone or worktree, never your everyday
  checkout.
- It always restores state, including after an error.
- It never approves or merges a PR.
- It skips PRs not authored by the acting user, draft PRs and closed PRs.
- It pushes only after the command passes locally, and only to the PR's own
  branch.
- It never tries a gate check (a name containing `gate` as a whole word, or `merge-reports`,
  case-insensitive). Gates never reach the agent and never use an attempt. It
  tries the other failing checks on the PR.
- It records a digest entry for exactly these cases:
    - the failure did not reproduce
    - the attempt cap was reached
    - the check output could not be parsed
    - a rebase is needed
    - the check is still failing after a fix
    - only gate checks are failing (`gate-only`)

The runner writes attempt counters and digest entries. This page only
describes them.

## The fix prompt

The fixing agent is instructed by `knowledge/pr-review/ci-fix-prompt.md`. The
runner uses that file as-is. Edit the file to change how the agent works.

## Known limitation

`gh pr checks` can exit with code 8 while still printing JSON. That
combination is not verified against live GitHub (PR #96). The command
reads the output the way `auto-approve` does: exit code 8 with empty output
means the status is unknown. Otherwise it parses the JSON. Output that is
not a list of check objects also means unknown. If parsing fails, the PR gets
a digest entry instead of a fix attempt.

## Errors

If any PR fails (a read failed, a git error, a push failed), the command logs
it. It continues with the other PRs. At the end, it exits non-zero with the
list of failures.
