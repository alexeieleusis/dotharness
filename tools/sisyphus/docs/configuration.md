# Configuration

Each target repository declares its Sisyphus settings in a `.sisyphus.toml` file.
`sisyphus.config.load_config(path)` loads and validates the file. It fails with a `ConfigError` that names
the section and key when the file has unknown keys, unsupported kinds, a missing `dir` for `file`, wrong value types, non-positive limits,
or mixed trackers.

## Path rule

Path values (`repo_dir`, `workspace_dir`, `brief_path_template`, `dir`, `digest.path_template` and the
`[knowledge]` paths) are returned exactly as written. Relative paths resolve against the invoking working
directory, not the directory that holds the config file. The loader never touches the filesystem for them.

## Secrets

The file names a command (`token_cmd`) and never holds a secret. The loader does not run `token_cmd` and does
not read environment variables.

## Sections

Only `[project]` is required. Every other section is optional. An absent optional key is `None` unless a
default is listed.

### `[project]`

| Key | Type | Default |
|---|---|---|
| `repo_dir` | string | none |
| `workspace_dir` | string | none |
| `brief_path_template` | string | none |
| `base_branch` | string | none (the CLI falls back to `main`) |

`base_branch` is the PR base branch. Precedence: `sisyphus run --base <branch>`, then `[project].base_branch`,
then `main`. `main` is the correct base for normal use. You need `--base` or this key only if you work on your own stacked branch (a branch whose PR targets
another unmerged branch). Set it to point at that parent branch.

### `[source.work_item]`

Declares the single tracker. Exactly one tracker is allowed.

| Key | Type | Default |
|---|---|---|
| `kind` | string, `linear`, `github` or `file` | none |
| `instruction` | string | none |
| `assignee` | string | none |
| `pick_state` | string (linear, or the producer for `file`) | none |
| `pick_label` | string (github, or the producer for `file`) | `sisyphus` when `kind = "github"`, else none |
| `dir` | string (file only, required for `file`) | none |

A `[source.linear]` or `[source.github]` table is rejected (as "two trackers" when `[source.work_item]` is
also present). `pick_state` with `kind = "github"` and `pick_label` with `kind = "linear"` are rejected as
mixed-tracker keys. `kind = "file"` accepts both, because the producer prompt reads them.

For `kind = "file"`, `dir` is required and names the directory of ticket files (`<dir>/<ref>.md`). Sisyphus
expands `~` in it. Sisyphus itself ignores `assignee`, `pick_state` and `pick_label` for `file`: they are the
parameters of the producer prompt (`knowledge/sisyphus/linear-feed-prompt.md`), which reads them from this table
to choose the Linear ticket to export (`assignee` and `pick_state` are required by the producer, `pick_label` is
optional). `dir` is rejected for any other kind.

### `[source.host]`

| Key | Type | Default |
|---|---|---|
| `token_cmd` | string | none |

### `[agent]`

| Key | Type | Default |
|---|---|---|
| `backend` | string, `claude` or `opencode` | none |

### `[limits]`

All values are positive integers.

| Key | Type | Default |
|---|---|---|
| `max_run_minutes` | integer | 180 |
| `max_tickets_per_run` | integer | 1 |
| `max_leaves_per_run` | integer | 4 |

### Ignored sections

`[source.ci]` and `[merge]` are read by harness (pr-review) and ignored by Sisyphus. They are accepted
in the file and have no effect.

### `[digest]`

| Key | Type | Default |
|---|---|---|
| `path_template` | string | none |

### `[knowledge]`

| Key | Type | Default |
|---|---|---|
| `sizing_file` | string | none |
| `conventions_path` | string | none |

## Minimal example

```toml
[project]
repo_dir = "."

[source.work_item]
kind = "github"

[limits]
max_tickets_per_run = 1
```
