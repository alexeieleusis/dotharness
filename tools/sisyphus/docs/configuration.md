# Configuration

Each target repository declares its Sisyphus settings in a `.sisyphus.toml` file, loaded by
`sisyphus.config.load_config(path)`. The file is validated when it is loaded: unknown keys, unsupported
kinds, wrong value types, non-positive limits and mixed trackers all fail with a `ConfigError` that names
the section and key.

## Path rule

Path values (`repo_dir`, `workspace_dir`, `brief_path_template`, `digest.path_template` and the
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

### `[source.work_item]`

Declares the single tracker. Exactly one tracker is allowed.

| Key | Type | Default |
|---|---|---|
| `kind` | string, `linear` or `github` | none |
| `instruction` | string | none |
| `assignee` | string | none |
| `pick_state` | string (linear only) | none |
| `pick_label` | string (github only) | `sisyphus` when `kind = "github"`, else none |

A `[source.linear]` or `[source.github]` table is rejected (as "two trackers" when `[source.work_item]` is
also present). `pick_state` with `kind = "github"` and `pick_label` with `kind = "linear"` are rejected as
mixed-tracker keys.

### `[source.ci]`

| Key | Type | Default |
|---|---|---|
| `kind` | string, `github` only | none |
| `max_fix_attempts` | positive integer | none |

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
| `max_fix_attempts_per_pr` | integer | 3 |
| `max_leaves_per_run` | integer | 4 |

### `[merge]`

Parsed and validated only; the merge runner is separate.

| Key | Type | Default |
|---|---|---|
| `enabled` | boolean | `false` |
| `require_all_files_viewed` | boolean | none |
| `max_changed_lines` | positive integer | none |

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

[source.ci]
kind = "github"

[limits]
max_tickets_per_run = 1
```
