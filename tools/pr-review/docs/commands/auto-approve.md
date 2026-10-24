# auto-approve

`auto-approve` is the one command that is allowed to conclude a review. For
pull requests where a review was requested from the current `gh` account, it
marks small files as viewed and approves small PRs, but only after the PR
author has answered the reviewer's comments. It is off until you set
`[auto_approve].enabled = true`.

Every other command only posts findings and leaves the decision to a human.
This one deliberately does not leave that to a human (see [`review-requested`](review-requested.md)).

## Usage
```
harness run [--config PATH] [--verbose] auto-approve [--dry-run]
```

- `--config PATH` — Path to the `.harness.toml` config file. Defaults to
  `./.harness.toml`. It belongs to the `run` group, so it must precede
  `auto-approve`.
- `--verbose` — Enables DEBUG-level logging and mirrors log output to stdout.
  Logs always go to `~/.local/share/dotharness/logs/auto-approve/<date>.log`.
- `--dry-run` — Does every read and every check, and logs what it would have
  marked or approved, but makes no GitHub write call.

When `[auto_approve].enabled` is `false` (the default), the command does
nothing and exits 0. It does not even take the lock.

## What it does

1. Takes the shared working-directory lock (see
   [Shared behavior](index.md#shared-behavior)). A second concurrent run exits
   immediately.
2. Lists the open, non-draft PRs where a review is requested from the current
   `gh` account. No other PRs are touched.
3. For each PR it checks out the branch, then measures how many lines changed
   per file and in total. Renamed, copied, deleted and binary files are not
   counted. Lockfiles are counted like any other text file.
4. Checks the timing rule, then the safety conditions. All reads happen before
   any write.
5. Marks each file with fewer than `viewed_max_lines` modified lines as
   viewed. It approves the PR if its total is fewer than `approve_max_lines`.
   A PR of any size is also approved once all of its files are viewed
   (marked by you in the GitHub UI, or by this command). That waives only the size limit. The timing rule, the safety conditions, the already-approved check and the head-SHA binding still apply. So the PR author must have commented or pushed after the review first.

Comparisons are strictly "less than". A threshold of `0` never matches, so it
turns that half of the feature off.

## The timing rule

Nothing happens until the PR author has responded to the reviewer. The
command looks at the account running it. Once that account has left comments, the PR author must comment or push a commit after the newest of them. Until then the
PR is left alone and checked again on the next run. This is why the command
is separate from the review commands: it needs to run on a later pass than the
one that posted the comments.

## The zero-findings rule

If the account left no inline review comments (zero findings), there is
nothing for the author to answer, so the PR does not wait for author activity.
It still waits until the summary, design and traceability comments exist.

## Safety conditions

Before approving, on the PR's current head commit, the command requires:

- the PR is open, not a draft, and has no merge conflicts;
- auto-merge is not enabled on it;
- CI is green. Required checks of the PR's base branch are used when it has
  them, and nothing else is read. A stacked PR's base is another open PR's
  branch, which has no required checks. For such a PR the command walks up the
  stack: it finds the open PR whose head branch is the base (matched by branch
  name only, no forks), then that PR's base, and so on until the base is the
  default branch or no open PR has it as head. Only the checks required at that
  stack root are judged on this PR, so a failing check that is not required
  does not block it. A required name with no run on the PR does not pass. If
  the root has no required checks, or there is no stack, all checks are used.
  The walk stops at 10 levels; a deeper stack, two open PRs with the same head
  branch, a failed or unreadable lookup, or pending required checks on the root
  count as unreadable and nothing is approved. Only the latest run of each check counts: runs are matched by
  workflow and check name, and the one with the latest completion time (or
  start time when it has not completed) decides. A failed run followed by a
  passing rerun does not block. A pending, failing or not-yet-completed latest
  run blocks approval. If a repeated check has a run with no usable timestamp,
  the checks count as unreadable. A PR with no checks at all passes this
  condition when nothing is required of it;
- the account has not already approved the PR.

If any condition does not hold, no approval is posted and the PR is checked
again on the next run. If a condition cannot be read (for example a `gh`
failure), it is not a pass: nothing is written for that PR this run.

## The approval

The approval is posted with an empty body, pinned to the head commit that was
checked. If the head moved while the command ran, the approval is skipped.
The command does not re-add the reviewer afterwards, so the review request may
clear. It does nothing after approving: later pushes are ignored, and a new
approval after more commits is manual.

## State and idempotency

The command is stateless. It reads GitHub on every run and keeps no state
file. Files already marked viewed are skipped, and a PR that the account has already approved is skipped, so re-running is safe.

## Errors

A `gh` failure (a read of the PR list, the timing gate, the safety checks or
the viewed states, or a failed approval or mark-viewed write) is logged as an
error, with `gh`'s stderr for the approval, and does not fail the run. A failed
read means no writes for that PR; every PR is checked again on the next run.

If a git error or an unexpected exception occurs, the command logs it, keeps
going with the other PRs (a git error stops the loop), and exits non-zero at
the end with the list of failures. In
`harness run all` this makes `all` exit non-zero.
