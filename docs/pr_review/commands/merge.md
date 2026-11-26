# merge

`merge` squash-merges your own open pull requests once all merge conditions
hold. It is off until you set `[merge].enabled = true` (`enabled = false` by
default). It never approves a PR (approving belongs to
[`auto-approve`](auto-approve.md)).

## Usage
```
pr_review run [--config PATH] [--verbose] merge [--dry-run] [--pr N]
```

The CLI command is `pr_review run merge`. `dotharness.pr_review.runners.merge.run(config, *, dry_run=False, pr=None)` is the entry point.

- `--dry-run` — Does every read and check, then logs `would merge` instead of merging. No merge call is made.
- `--pr N` — Processes only PR number `N`. Without it, every open PR authored by the acting user is processed.

When `[merge].enabled` is `false`, the command logs `merge is disabled, nothing to do` and returns. It makes no `gh` call and does not take the lock.

## Merge conditions

A PR is merged only when all of these hold. They are checked in this order. The first failure logs one skip reason. No merge call is made.

1. The PR was authored by the acting user (a literal login comparison. A blank acting user never matches.)
2. The PR is open, not a draft, has no conflict, and has no auto-merge request.
3. Required checks pass (or all checks when none are required). For a stacked PR the required checks are those of the stack root, as in [auto-approve](auto-approve.md#safety-conditions). A required check with no run on the PR does not pass. Only the latest run of each check counts, as in [auto-approve](auto-approve.md#safety-conditions). A PR with no checks passes only when the repo has no active workflows.
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
| `ci-blocked` | The latest run of a required check (or of any check when none are required) is failing, pending or not completed, or a required check has no run |
| `checks_read_failed` | The checks could not be read |
| `gate_record_missing` | No readable gate record for the PR |
| `gate_record_stale` | The gate record's head SHA differs from the PR's head |
| `size_read_failed` | The changed-line count could not be read |
| `over_max_changed_lines` | Changed lines exceed `max_changed_lines` |
| `unviewed_files` | The file set is empty or a file is not viewed |
| `viewed_read_failed` | The viewed states could not be read |

A failed `gh pr merge` call, or an unexpected exception, is an error. The run continues with the other PRs and then raises `MergeError` listing the failed PRs.

## Config

```toml
[merge]
enabled = false
require_all_files_viewed = true
max_changed_lines = 500
```

Unknown keys are rejected. See [configuration](../configuration.md).

## Gate record

The sisyphus gate-record writer (`write_gate_record` in `dotharness.sisyphus.gaterecord`) writes the evidence that `build.commands` passed on the head SHA. It is called from `_Run.gate()` in the sisyphus orchestrator. The path comes from `dotharness.paths.build_gate_record_path`, which this reader also uses, so the reader and the writer cannot drift apart:

- File: `~/.local/share/dotharness/state/<repo-slug>/build_gate_<PR number>.json`
- Content: `{"head_sha": "<sha>"}` (other keys are ignored)

When a record is written:

- After the gate passes, sisyphus writes a record for the head SHA it read before the gate ran. The write is atomic, and rewriting the same PR overwrites the record.
- When the gate does not run (no `build.commands` configured, no changed files, or no applicable build entry), sisyphus still writes a record for the current head SHA. It then adds the note `gate record written for PR #N (gate not run: <reason>)` to the digest.
- A failed gate writes no record.
- If the record cannot be written, sisyphus adds the note `gate record not written for PR #N: <error>` to the digest and carries on. The gate outcome is unchanged.

### `<repo-slug>` and `[project] repo_name`

`<repo-slug>` is the directory name under `~/.local/share/dotharness/state/`. For sisyphus it comes from the optional `[project] repo_name` key:

- When `repo_name` is unset, `<repo-slug>` falls back to the sanitised basename plus 8-hex hash of `project.repo_dir`.
- When `repo_name` is set, the value has `/` replaced by `-` (so `owner/repo` becomes `owner-repo`). The result must match `[a-zA-Z0-9._-]+` and must not be `.` or `..`. An invalid value is a config error.

`merge` derives its own slug from pr_review's `[repo] name` (required for `merge`; `/` becomes `-`, see [configuration](../configuration.md#repo)). The shared path function does not make the two slugs agree. The configs must. Set `[project] repo_name` to the same value as pr_review's `[repo] name` (for example `acme/frontend` in both) so that both sides resolve to `acme-frontend`.

The unset fallback (basename plus 8-hex hash) can never equal a name-derived slug such as `owner-repo`. It only matches a pr_review config that has no `name` (`local-review`), so it is effectively unusable with `merge`. If the slugs differ, sisyphus writes the record but `merge` looks elsewhere and reports `gate_record_missing`.

A missing, unreadable or SHA-mismatched record blocks the merge.
