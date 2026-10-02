# local-review

`local-review` reviews the branch you have checked out in `repo.working_dir`, before you push or open a pull request. It does not use GitHub. The configured AI backend reviews each changed file, then writes one summary and one design review. All results go to Markdown files on disk. Nothing is posted anywhere.

The command has two phases. The review phase writes findings. The optional address phase (`--address`) then asks the backend to fix each open finding and to commit the fix on your branch. See [Address phase and finding curation](#address-phase-and-finding-curation).

## Usage
```
harness run [--config PATH] [--verbose] local-review [--base REF] [--output-dir DIR] [--force] [--address [--skip-review [--review-dir DIR]]]
```

- `--config PATH` — path to the `.harness.toml` config file. Defaults to `./.harness.toml`. This option belongs to the `run` group, so it must come before `local-review` on the command line.
- `--verbose` — enables DEBUG-level logging. It also streams logs to stdout. Logs are written to `~/.local/share/dotharness/logs/local-review/<date>.log` regardless of this flag.
- `--base REF` — the base ref to compare against. See [Base ref and diff](#base-ref-and-diff).
- `--output-dir DIR` — the output root. See [Output layout](#output-layout).
- `--force` — ignore earlier results for this head commit and run every pass again. See [Re-runs](#re-runs).
- `--address` — after the review phase, address every open finding. See [Address phase and finding curation](#address-phase-and-finding-curation).
- `--skip-review` — only valid with `--address`. Do not run the review phase. Address the findings of an earlier review instead.
- `--review-dir DIR` — only valid with `--address --skip-review`. Address this review directory instead of choosing one.

`--skip-review` without `--address`, `--review-dir` without `--address`, and `--review-dir` without `--skip-review` are usage errors. They exit with code 2 before anything runs.

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

10. With `--address`, runs the address phase after the review phase (or instead of it, with `--skip-review`). See [Address phase and finding curation](#address-phase-and-finding-curation).

The command prints the base ref and merge base, the head commit, the review directory, and the status of each pass (`per-file: N/M done`, `summary`, `design`). With `--address` it also prints the review directory it addresses, how many findings are open, and a one-line summary of the outcomes.

With `--skip-review`, the checks on the review prompt templates and the "nothing to review" check are skipped. Only the git checks, the output directory check, and the address template check apply.

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

Only these `.harness.toml` fields affect this command. See [Configuration](../configuration.md) for the full schema.

| Field | Used for |
|---|---|
| `[local_review].base` | Default base ref |
| `[local_review].output_dir` | Default output root |
| `harness.backend` | Which AI backend (`opencode` or `claude`) runs the passes |
| `harness.backend_timeout_seconds` | Timeout for each backend invocation |
| `harness.knowledge_dir` | Must contain `pr-review/local-review-file.md`, `pr-review/local-review-summary.md` and `pr-review/local-review-design.md`. With `--address` it must also contain `pr-review/local-address-finding.md`. With `--skip-review` the three review templates are not needed |
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

With `--address`, add one backend call for each `open` finding. There are no other backend calls. A re-run for the same head commit only repeats failed passes. A file that keeps failing makes every re-run send that file again, plus the summary, which always re-runs after a file failure. With an expensive backend, a large change, and a file that keeps failing, this can approach twice the normal cost. Set `harness.backend_timeout_seconds` conservatively. Check `~/.local/share/dotharness/logs/local-review/` for timeout patterns.

The address phase retries nothing by itself. A finding that ends `failed` keeps `status: open`, so the next `--address` run sends it again and pays for it again. If a finding keeps failing, set its status to `wontfix` yourself (see [Address phase and finding curation](#address-phase-and-finding-curation)). `harness.backend_timeout_seconds` applies to each address call too.

## Security

The backend runs with unrestricted shell access in `repo.working_dir`. For `claude`, the tool passes `--dangerously-skip-permissions`. The backend can run any command and read or write any file in the checkout, including `.git/`. See [Security in `self-review`](self-review.md#security) for the backend mitigations that also apply here.

- **Mutation guard.** After every backend call, the tool checks that `HEAD` did not move and that no tracked file changed. Untracked files are ignored. If the check fails, that pass is marked `failed`, no further passes run, and the command exits non-zero. The message names what changed. The guard detects changes. It does not undo them. Inspect the repository and restore it yourself.
- **The address phase edits your branch.** With `--address`, the backend can create commits on your checked-out branch. The tool never pushes, but the commits are real and stay in your history until you remove them. Review them (`git log`, `git show`) before you push.
- **Address checks detect, they do not undo.** After every backend call in the address phase, the tool checks that the old `HEAD` is still an ancestor of the new `HEAD` (so history was not rewritten), that the branch did not change, and that no tracked file has uncommitted changes. If a check fails, the finding is marked `failed`, the phase stops, and the command exits non-zero. The tool resets and repairs nothing. Inspect the repository and restore it yourself.
- **No `GITHUB_TOKEN`.** The tool does not export `GITHUB_TOKEN` to the backend. This is a smaller exposure than `self-review`. The backend can still reach the network if the host allows it.
- **Output outside the repo.** The command refuses an output directory inside `repo.working_dir` (including the directory itself). This keeps review files out of your working tree and out of the guard check.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | All passes are `done`, or there is nothing to review. With `--address`, no finding failed, was unparseable, or stopped the phase |
| `1` | A precondition failed, a pass ended `failed`, the mutation guard tripped, no review directory could be chosen for `--address`, or the address phase had a failed or unparseable finding or stopped early |
| `2` | A usage error, such as `--skip-review` without `--address` |

## Scheduling

`local-review` is not part of `harness run all`. To run it on a schedule, use:

```
harness schedule install local-review --every <duration> [--config PATH] [--scheduler cron|launchd]
```

## Address phase and finding curation

`--address` asks the backend to deal with each `open` finding, one finding at a time. The tool does not trust what the backend says. It checks the result with git before it records anything.

Examples:

```
# Review, then address the findings of this review
harness run local-review --address

# Edit the findings first, then address the latest earlier review without reviewing again
harness run local-review --address --skip-review

# Address one specific earlier review
harness run local-review --address --skip-review --review-dir ~/.local/share/dotharness/reviews/<repo_slug>/<branch>/<head_sha>
```

### Which review is addressed

- With `--address` alone, the review phase runs first (or is skipped for `done` passes, as usual). The address phase then uses the review directory for the current head commit.
- With `--address --skip-review`, the review phase does not run. The tool picks the review directory of the same branch whose head commit is `HEAD` or an ancestor of `HEAD` and has the newest committer date. If two have the same date, the one with the newer `manifest.json` modification time wins. If there is none, the command exits with an error that tells you to run `local-review` first.
- With `--review-dir DIR`, the tool uses that directory. It must contain a `manifest.json` for the same branch, and its head commit must be `HEAD` or an ancestor of `HEAD`. Otherwise the command exits with an error.

Only passes marked `done` in the manifest are read. If some review passes failed, the address phase still runs on the passes that are `done`, and the command exits non-zero at the end.

### Curating findings

The address phase acts only on findings with `status: open`. Before you run it, you can open the review files and edit them:

| Status | What the address phase does |
|---|---|
| `open` | Sends the finding to the backend. |
| `wontfix` | Skips it. Use this for findings you reject. |
| `fixed` | Skips it. The address phase sets this and adds a `- commit: <sha>` line. |
| `declined` | Skips it. The address phase sets this when the backend decided that the finding does not need a change. |

To drop a finding, set `status: wontfix` or delete the whole block. Do not change the `id` line. A block with a missing or invalid field, or with no `id`, cannot be used. The tool counts it as unparseable, ignores it, and exits non-zero at the end.

Findings are addressed in this order: the per-file findings, by file path and then by line, and then the design findings in file order.

### What happens for each finding

1. The tool checks that you are still on the branch that was reviewed and that no tracked file has uncommitted changes. Untracked files are ignored. If not, the finding is marked `failed` and the phase stops.
2. It records the current `HEAD` and runs the backend once. The prompt contains the instructions from `local-address-finding.md`, the finding, its file and line, the diff of that file against the merge base, any vibe-heal context, and the path where the backend must write its resolution.
3. The backend writes a resolution file, `<review dir>/resolutions/<finding id>.md`. Its first line must be `decision: fixed` or `decision: declined`. The tool deletes any old resolution file for the finding before the call.
4. The tool compares the decision with git:

| Decision | What git must show | Result |
|---|---|---|
| `fixed` | Exactly one new commit on top of the recorded `HEAD` (not a merge commit) and no uncommitted changes to tracked files | The finding gets `status: fixed` and a `- commit: <sha>` line |
| `declined` | `HEAD` did not move and no uncommitted changes to tracked files | The finding gets `status: declined` |
| Anything else | A missing, empty or malformed resolution file, a backend error or timeout, no commit or two commits for `fixed`, or a commit for `declined` | The finding is `failed` and keeps `status: open` |

A `failed` finding is not a status in the file. It is only reported in the summary line.

### Stopping the phase

After every backend call, the tool also checks that the recorded `HEAD` is still an ancestor of the new `HEAD`, that you are still on the same branch, and that no tracked file has uncommitted changes. If any check fails, the finding is marked `failed`, no further backend call is made, and the command exits non-zero. The remaining findings are not tried. They count as "skipped (not open)" in the summary line. The tool does not reset or repair anything. See [Security](#security).

### Commit messages

The backend writes the commit. The runner does not rewrite or check the message. The instructions in `local-address-finding.md` tell the backend to use this format:

```
fix: address review finding <id> [on <path/to/file.ext>]

- <brief description of what was changed>

Ref: local-review finding <id> (reviewed at <head sha of the review, 12 chars>)
```

The backend is told to create a new commit on top of `HEAD`, to stage only the files it changed, and never to amend, rebase, reset, push or use `--no-verify`. If pre-commit hooks in your repository fail, the commit does not happen and the finding is `failed`.

### Output and exit code

The last line of the phase is:

```
findings: 3 fixed, 1 declined, 1 failed, 2 skipped (not open), 0 unparseable
```

`skipped (not open)` counts findings with a status other than `open`, plus any open findings that were not tried because the phase stopped early. The exit code is `0` only if nothing failed, no block was unparseable and the phase did not stop early.

### Re-runs

Only `open` findings are addressed again. A `fixed` or `declined` finding stays as it is. A `failed` finding keeps `status: open`, so `--address` tries it again on the next run. Each retry costs one backend call (see [Cost](#cost)).

`--force` re-runs the review passes and overwrites the review files. This replaces the statuses set by the address phase, and the tool logs a warning with the number of `fixed` and `declined` findings that are lost. The commits made by earlier address runs stay in your git history.

`harness state reset local-review` deletes the whole review output, including the resolution files. It does not touch your commits.
