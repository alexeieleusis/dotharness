# merge

`merge` squash-merges your own open pull requests once every merge condition
holds. It is off until you set `[merge].enabled = true` (`enabled = false` by
default). It never approves a PR (approving belongs to
[`auto-approve`](auto-approve.md)).

## Usage
```
harness run [--config PATH] [--verbose] merge [--dry-run] [--pr N]
```

The CLI wiring is a separate change. `harness.runners.merge.run(config, *, dry_run=False, pr=None)` is the entry point.

- `--dry-run` — Does every read and check, then logs `would merge` instead of merging. No merge call is made.
- `--pr N` — Processes only PR number `N`. Without it, every open PR authored by the acting user is processed.

When `[merge].enabled` is `false`, the command logs `merge is disabled, nothing to do` and returns. It makes no `gh` call and does not take the lock.

## Merge conditions

A PR is merged only when all of these hold. They are checked in this order. The first failure logs one skip reason. No merge call is made.

1. The PR was authored by the acting user (a literal login comparison. A blank acting user never matches.)
2. The PR is open, not a draft, has no conflict, and has no auto-merge request.
3. Required checks pass (or all checks when none are required).
4. The gate record exists and its head SHA equals the PR's current head SHA.
5. Changed lines (additions plus deletions, from `gh pr view --json additions,deletions`) are at most `max_changed_lines`.
6. When `require_all_files_viewed`: the file list is non-empty and every file is marked viewed. Viewed state is read fresh on every run and never cached. A push after the last viewed mark clears it on GitHub.

The merge call is `gh pr merge N --repo R --squash --match-head-commit <sha>`, pinned to the head SHA the runner evaluated. Squash is the only method. The runner never uses `--admin`, `--auto` or `--delete-branch`, and never approves.

## Skip reasons

Each skip is logged as `PR #N: skipped: <reason>`. A summary of every PR's outcome is logged at the end. A skip is not an error.

| Reason | Meaning |
|---|---|
| `not_author` | The PR was not authored by the acting user |
| `state_read_failed` | The PR state could not be read |
| `not-open` | The PR is not open |
| `draft` | The PR is a draft |
| `conflicting` | The PR has a merge conflict |
| `mergeable-unknown` | GitHub has not computed mergeability yet |
| `auto-merge` | An auto-merge request is already set |
| `ci-blocked` | A required check is failing or pending |
| `checks_read_failed` | The checks could not be read |
| `gate_record_missing` | No readable gate record for the PR |
| `gate_record_stale` | The gate record's head SHA differs from the PR's head |
| `size_read_failed` | The changed-line count could not be read |
| `over_max_changed_lines` | Changed lines exceed `max_changed_lines` |
| `unviewed_files` | The file set is empty or a file is not viewed |
| `viewed_read_failed` | The viewed states could not be read |

A failed `gh pr merge` call, or an unexpected exception, is an error: the run continues with the other PRs and then raises `MergeError` listing the failed PRs.

## Config

```toml
[merge]
enabled = false
require_all_files_viewed = true
max_changed_lines = 500
```

Unknown keys are rejected. See [configuration](../configuration.md).

## Gate record

Package A (`tools/sisyphus`, `gaterecord.py`) writes the evidence that `build.commands` passed on the head SHA. The format is settled. The reader here and the writer there must stay in sync:

- File: `~/.local/share/dotharness/state/<repo-slug>/build_gate_<PR number>.json`
- Content: `{"head_sha": "<sha>"}` (other keys are ignored)

A missing, unreadable or SHA-mismatched record blocks the merge.
