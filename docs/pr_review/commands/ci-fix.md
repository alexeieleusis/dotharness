# ci-fix

`ci-fix` fixes CI overnight on your own open pull requests. For each PR you
authored whose required checks have failed, it reproduces the failure locally.
It makes the smallest fix. It then pushes the fix to the PR's own branch to
make CI green. A PR with merge conflicts against its base is rebased onto
the base branch instead (see [What it does](#what-it-does)).
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
- `--dry-run` — Does every read and runs the failing-check commands locally.
  Makes no commit and no push. Attempt counters are unchanged. For a PR with
  merge conflicts it only logs that a rebase would run. It does not check out
  or rebase, and writes no digest line for it.
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

A PR with merge conflicts against its base (CI failing or green) takes the
rebase path instead of steps 3 to 10. A PR that is only behind its base, with
no conflict, is not rebased. A PR whose mergeable state is unknown is not
rebased either. It is retried on the next run.

1. Checks the attempt cap. At the cap it records `cap-reached` before any
   checkout.
2. Fetches `origin`, checks out the PR branch and records its head SHA.
3. Runs `git rebase origin/<base>`.
4. If git stops on a conflict, runs the backend once per stopped commit. The
   agent only edits and stages the conflicted files, using the rebase prompt
   (`knowledge/pr-review/ci-fix-rebase-prompt.md`). The runner runs
   `git rebase --continue` itself. This repeats until the rebase completes.
   If the agent fails, leaves conflict markers or `--continue` does not
   advance, the runner aborts the rebase and pushes nothing.
5. Vets the rebased tree against `origin/<base>` with the same unsafe-change
   check as a fix. Then it reads the CI status:
    - CI failing: runs the failing step command on the rebased tree. It must
      exit 0.
    - CI green, or only gate checks failing: runs no command. A clean rebase is
      pushed as is. For gate-only it logs that the clean rebase has no step to
      verify.
6. Writes the recovery ref `refs/harness-recovery/<branch>-<sha12>` (the old
   head) and pushes with
   `git push --force-with-lease=<branch>:<pre-rebase-sha> origin <branch>`.
   This push ends the PR for the run. A failing check that remains is fixed on
   a later run.
7. Restores state with `git_restore`. It also aborts a rebase left in
   progress.

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
- A run of the rebase path that ran the backend counts as exactly one attempt
  in the same `max_fix_attempts` counter, however many commits stopped. A clean
  rebase counts none. The cap is checked before the checkout and yields
  `cap-reached`.
- There is no limit on stopped commits or conflicted files. Each backend run is
  bound only by the backend timeout.
- The green-CI counter reset is skipped while the PR has merge conflicts.
- A rebase push ends the processing of that PR for the run. If CI was failing,
  the command was verified on the rebased tree before the push.
- Pushes are plain `git push origin <branch>` to the PR's own branch. The only
  exception is the rebase path: `git push
  --force-with-lease=<branch>:<pre-rebase-sha> origin <branch>`, after the
  recovery ref `refs/harness-recovery/<branch>-<sha12>` is written. The
  recovery ref is never deleted. ci-fix rebases only in-scope PRs onto
  `origin/<base>` and pushes no other ref.

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
- It pushes a fix only after the command passes locally. It pushes a rebase
  only after the rebased tree is vetted and, if CI is failing, the failing
  step command passes locally. A green or gate-only PR gets a clean rebase
  pushed with no command run. It pushes only to the PR's own branch.
- The only force-push is
  `git push --force-with-lease=<branch>:<pre-rebase-sha> origin <branch>` on
  the rebase path, after the recovery ref exists. All other pushes are plain.
- It never tries a gate check (a name containing `gate` as a whole word, or `merge-reports`,
  case-insensitive). Gates never reach the agent and never use an attempt. It
  tries the other failing checks on the PR.
- It records a digest entry for exactly these cases:
    - the failure did not reproduce
    - the attempt cap was reached
    - the check output could not be parsed
    - a push was rejected because the branch moved on the remote (`rebase-needed`)
    - the check is still failing after a fix
    - only gate checks are failing (`gate-only`)
    - the PR was rebased and pushed (`rebased`)
    - the rebase conflict was not resolved (`rebase-conflict-unresolved`)
    - the rebased tree failed the safety vetting (`rebase-unsafe`)
    - the check still fails on the rebased tree (`rebase-still-failing`)

Each digest is a log line
`ci-fix digest: <outcome> pr=<n> check=<name> <detail>`. The four rebase
outcomes below have this content. A rebase digest with no failing check shows
`check='-'`. That is the no-check sentinel.

- `rebased`: `check='-'`, detail `recovery_ref=<ref> stopped_commits=<n>`.
- `rebase-conflict-unresolved`: `check='-'`, detail one of
  `backend failed: <error>`, `conflict markers left in: <files>`,
  `git rebase --continue did not advance: <git's last stderr line>`, or
  `git rebase origin/<base> failed` (git failed with no conflict to resolve).
  The rebase is aborted and nothing is pushed.
- `rebase-unsafe`: `check='-'`, detail names the unsafe change (the path).
  The rebase is aborted and nothing is pushed.
- `rebase-still-failing`: `check=<failing check name>`, detail
  `step <name!r> still fails (exit N)`. The rebase is aborted and nothing is
  pushed.

Two existing outcomes also appear on the rebase path with `check='-'`:
`cap-reached` (detail `attempts=<n> max=<m>`) and `parse-failure` (detail
`could not read the check status`, or `could not write the recovery ref`, in
which case nothing is pushed). A failing check that has no runnable command on
the rebased tree gives `parse-failure` with that check's name. `rebase-needed`
here has `check='-'` and detail `push rejected, the branch moved on the remote`.
The recovery ref is kept. A dry run writes no digest line.

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

A git failure while checking out a PR branch (for example a timeout) skips that
PR and is logged at ERROR as `PR #N skipped: git failure: <first line of git's
stderr>`. The working directory is then cleaned and probed with
`git status --porcelain`. If the cleanup succeeded and the tree is clean, the
failure is identified and the command continues with the next PR. Otherwise the
loop stops. Either way, a cycle that skipped a PR for a git failure never exits
0, and the aggregate error lists the skipped PR numbers.
