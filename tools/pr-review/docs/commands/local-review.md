# local-review

`local-review` reviews the branch you have checked out in `repo.working_dir`, before you push or open a pull request. It does not use GitHub. The configured AI backend reviews each changed file, then writes one summary and one design review. All results go to Markdown files on disk. Nothing is posted anywhere.

This page documents the review phase. The address phase, which acts on the findings, is documented separately (see [Address phase and finding curation](#address-phase-and-finding-curation)).

## Usage
```
harness run [--config PATH] [--verbose] local-review [--base REF] [--output-dir DIR] [--force]
```

- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml`. This option belongs to the `run` group, so it must come before `local-review` on the command line.
- `--verbose` — enables DEBUG-level logging. It also streams logs to stdout. Logs are written to `~/.local/share/dotharness/logs/local-review/<date>.log` regardless of this flag.
- `--base REF` — the base ref to compare against. See [Base ref and diff](#base-ref-and-diff).
- `--output-dir DIR` — the output root. See [Output layout](#output-layout).
- `--force` — ignore earlier results for this head commit and run every pass again. See [Re-runs](#re-runs).

`repo.name` is optional for this command, because it does not need a GitHub repository. See [Configuration](../configuration.md).

## What it does

1. Acquires the same exclusive file lock as the other commands, keyed on the resolved `repo.working_dir` path (see [Shared behavior](index.md#shared-behavior)). A second run against the same working directory exits with an error.
2. Resolves the output root and refuses to continue if it is inside `repo.working_dir` (see [Security](#security)).
3. Checks the git preconditions. Each failure exits non-zero with a message on stderr:
    - `repo.working_dir` is the top level of a git repository.
    - `HEAD` is on a branch, not detached.
    - There are no uncommitted changes to tracked files. Untracked files are ignored. Uncommitted changes are not reviewed, so commit or stash them first.
    - The base ref resolves (see [Base ref and diff](#base-ref-and-diff)).
4. If the branch has no commits beyond the base, it prints `Nothing to review: <branch> has no commits beyond <base ref>` and exits 0.
5. Loads the prompt templates `local-review-file.md`, `local-review-summary.md` and `local-review-design.md` from `harness.knowledge_dir/pr-review/`. It also loads the optional `harness.review_knowledge_file`, which is added to every prompt as an "Additional Review Guide" section.
6. For each changed file that is not yet `done`, builds a prompt from the file diff (or the whole file, if the file is new or the diff touches 75% or more of it). The prompt also holds a description of the change (the branch commits) and any matching vibe-heal static-analysis context. It runs the backend once. The backend writes its findings to `files/<changed file path>.md`.
7. Runs one summary pass, then one design pass, unless each is already `done`. The design pass gets the diffs of all changed files in one prompt. The summary pass runs even if some file passes failed, but it is then recorded as `failed`, so it runs again next time together with the failed files.
8. After each pass, it rewrites `manifest.json`. After the last pass, it writes `index.md`.
9. Checks, after every backend call, that the backend did not change the repository (see [Security](#security)).

The command prints the base ref and merge base, the head commit, the review directory, and the status of each pass (`per-file: N/M done`, `summary`, `design`).

## Base ref and diff

The base ref is the first of these that exists:

1. `--base`
2. `[local_review].base`
3. `refs/remotes/origin/HEAD`, if it is present locally
4. `main`

If `--base` or `[local_review].base` is set and does not resolve, the command fails. It does not fall back to the next item.

The merge base is `git merge-base <base ref> HEAD`. The review diff is `git diff <merge base> HEAD`. The command does not fetch from the network.

## Output layout

The output root is the first of these:

1. `--output-dir`
2. `[local_review].output_dir`
3. `~/.local/share/dotharness/reviews`

The review directory is:

```
<output root>/<repo_slug>/<branch>/<head_sha>/
```

A `/` in the branch name stays a directory separator. `repo_slug` comes from `repo.name`. When `repo.name` is absent, it is the working directory name plus a short hash of its path (see [Configuration](../configuration.md)).

The review directory contains:

| Path | Content |
|---|---|
| `files/<changed file path>.md` | Findings for one changed file. The file reads `No P0/P1 findings.` when there are none. |
| `summary.md` | Overall summary of the change. |
| `design.md` | Design and architecture review of the whole change. |
| `manifest.json` | Base ref, merge base, head commit, and the status of each pass (`done` or `failed`). |
| `index.md` | Human-readable overview with links to the other files. |

## Finding format

Each finding is a block in a `files/*.md` file or in `design.md`. A block starts with a heading at column 0 and ends at the next column-0 `##` heading or at the end of the file:

```markdown
## Finding: <short title>
- severity: P0 | P1
- file: <path relative to the repo root>
- line: <number; 1 if unknown>
- status: open

Free text that explains the problem and the fix.
```

After the backend finishes, the tool adds one more line to each block:

```markdown
- id: <pass>-<8 hex>
```

`<pass>` is `file` or `design`. The eight hex characters come from a hash of the block text, so the ID stays the same on later runs. The tool never changes an existing `id`. Summary files get no IDs.

Status values:

| Status | Meaning |
|---|---|
| `open` | A new finding. This is the only status the backend writes. |
| `wontfix` | Set by you, to exclude the finding. |
| `fixed` | Set by the address phase when the finding is fixed. |
| `declined` | Set by the address phase when the finding is not fixed. |

You curate findings by editing the `status` line or by deleting the whole block. A deleted block no longer exists, and nothing brings it back unless you re-run the pass with `--force`.

## Re-runs

A pass is `done` only when both of these are true:

- The backend exited 0 without timing out.
- For the summary and design passes, the output file exists and is not empty.

A file pass that exits 0 without writing a file is `done`, and the tool writes `No P0/P1 findings.` for it.

If you run the command again for the same head commit, `done` passes are skipped, and only `failed` or missing passes run. `--force` ignores the manifest and runs every pass again. It overwrites the output files, including any status you edited by hand.

A new commit gives a new head commit, so it gets a new review directory. The tool never deletes old review directories.

To delete all reviews of this repo, run:
```
harness state reset local-review [--config PATH] [--yes]
```
This deletes `<output root>/<repo_slug>/` after you confirm. `--yes` skips the confirmation. If the directory does not exist, the command says so and exits 0.

## Configuration

Only these `.harness.toml` fields affect the review phase. See [Configuration](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `[local_review].base` | Default base ref |
| `[local_review].output_dir` | Default output root |
| `harness.backend` | Which AI backend (`opencode` or `claude`) runs the passes |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation |
| `harness.knowledge_dir` | Must contain `pr-review/local-review-file.md`, `pr-review/local-review-summary.md` and `pr-review/local-review-design.md` |
| `harness.review_knowledge_file` | Optional extra guidance added to every prompt, if the path exists |
| `harness.path_prepend` / `harness.env` | Extra `PATH` entries and env vars for git and the backend |
| `repo.working_dir` | The git checkout to review. Its resolved path is also the basis of the lock key |
| `repo.name` | Optional. Used only for the `repo_slug` and context text |
| `repo.subdir[].path` | Used to find a matching vibe-heal `review.md` as static-analysis context |

`harness.gh_token_cmd` is not used.

## Cost

Backend invocations per run:

- One per changed file that is not yet `done`.
- One for the summary, if it is not `done`.
- One for the design review, if it is not `done`.

There are no other backend calls. A re-run for the same head commit only repeats failed passes. A file that keeps failing makes every re-run send that file again, plus the summary, which always re-runs after a file failure. With an expensive backend, a large change, and a file that keeps failing, this can approach twice the normal cost. Set `harness.backend_timeout_seconds` conservatively. Check `~/.local/share/dotharness/logs/local-review/` for timeout patterns.

## Security

The backend runs with unrestricted shell access in `repo.working_dir`. For `claude`, the tool passes `--dangerously-skip-permissions`. The backend can run any command and read or write any file in the checkout, including `.git/`. See [Security in `self-review`](self-review.md#security) for the backend mitigations that also apply here.

- **Mutation guard.** After every backend call, the tool checks that `HEAD` did not move and that no tracked file changed. Untracked files are ignored. If the check fails, that pass is marked `failed`, no further passes run, and the command exits non-zero. The message names what changed. The guard detects changes. It does not undo them. Inspect the repository and restore it yourself.
- **No `GITHUB_TOKEN`.** The tool does not export `GITHUB_TOKEN` to the backend. This is a smaller exposure than `self-review`. The backend can still reach the network if the host allows it.
- **Output outside the repo.** The command refuses an output directory inside `repo.working_dir` (including the directory itself). This keeps review files out of your working tree and out of the guard check.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All passes are `done`, or there is nothing to review |
| non-zero | A precondition failed, a pass ended `failed`, or the mutation guard tripped |

## Scheduling

`local-review` is not part of `harness run all`. To run it on a schedule, use:

```
harness schedule install local-review --every <duration> [--config PATH] [--scheduler cron|launchd]
```

## Address phase and finding curation

Acting on the findings (the address phase) is documented separately.
