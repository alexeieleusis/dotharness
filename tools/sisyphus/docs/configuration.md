# Configuration

Each target repository declares its Sisyphus settings in a `.sisyphus.toml` file.
`sisyphus.config.load_config(path)` loads and validates the file. It fails with a `ConfigError` that names
the section and key when the file has unknown keys, unsupported kinds, a missing `dir` for `file`, wrong value types, non-positive limits,
or mixed trackers.

## Path rule

Path values (`repo_dir`, `workspace_dir`, `brief_path_template`, `[source.work_item]` `dir`,
`digest.path_template`, `[archive]` `dir` and the `[knowledge]` paths) are returned exactly as written.
Relative paths resolve against the invoking working directory, not the directory that holds the config file.
The loader never touches the filesystem for them. The one exception is `[archive] dir`, which commands resolve
against `[project].workspace_dir` (see [`[archive]`](#archive)).

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

In `brief_path_template`, `<item-id>` is the folded stem of the work-item reference: the reference is
ASCII-lower-cased and escaped, substituted as `template.replace("<item-id>", encode_item(item))`. For example
`#147` gives `_23147` and the mixed-case `ENG-12` gives `eng-12`, so the brief is `<workspace_dir>/eng-12.brief.md`
with the template above. References that differ only in ASCII case (`ENG-12` and `eng-12`) are one work item.

### `[source.work_item]`

Declares the single tracker. Exactly one tracker is allowed.

| Key | Type | Default |
|---|---|---|
| `kind` | string, `linear`, `github` or `file` | none |
| `instruction` | string | none |
| `assignee` | string | none |
| `pick_state` | string (linear, or the producer for `file`) | none |
| `pick_label` | string (github, or the producer for `file`). For github Sisyphus reads it to pick and writes it: it removes the label when the item settles | `sisyphus` when `kind = "github"`, else none |
| `remove_label_when_settled` | bool (github only, needs `pick_label`; rejected for `linear`, ignored for `file`) | `true` when `kind = "github"`, else none |
| `dir` | string (file only, required for `file`) | none |

A `[source.linear]` or `[source.github]` table is rejected (as "two trackers" when `[source.work_item]` is
also present). `pick_state` with `kind = "github"` and `pick_label` with `kind = "linear"` are rejected as
mixed-tracker keys. `kind = "file"` accepts both, because the producer prompt reads them.

For `kind = "file"`, `dir` is required and names the directory of ticket files (`<dir>/<ref>.md`). Sisyphus
expands `~` in it. Sisyphus itself ignores `assignee`, `pick_state` and `pick_label` for `file`: they are the
parameters of the producer prompt (`knowledge/sisyphus/linear-feed-prompt.md`), which reads them from this table
to choose the Linear ticket to export (`assignee` and `pick_state` are required by the producer, `pick_label` is
optional). `dir` is rejected for any other kind.

For `kind = "github"`, Sisyphus both reads and writes `pick_label`. With `remove_label_when_settled` on (the
default), it removes the label from the issue when the item settles: when the finish trigger has opened the
draft PR, or when you run `sisyphus abandon #<number>`. A removal failure is a digest note (finish) or one stderr
line (abandon), never a stop and never a changed exit code. Re-queuing is a manual relabel; `resume --again` does
not restore the label, and items settled earlier are not back-filled. Set `remove_label_when_settled = false` to
keep the label. Merged or closed PRs still do not exclude an issue from the pick.

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

### `[archive]`

Optional.

| Key | Type | Default |
|---|---|---|
| `dir` | string | `archive` |

`sisyphus archive --config F` moves the files of settled items from `[project].workspace_dir` into the archive
directory (see [`sisyphus archive`](usage.md#sisyphus-archive)). The layout is flat and keeps the original
names, for example `<archive>/_23147.json` and `<archive>/plan-147/`. The directory is created on first use.

- A relative `dir` resolves against `[project].workspace_dir`, not the invoking working directory, so the
  default is `<workspace_dir>/archive`. This is an exception to the Path rule above. A relative `dir` needs
  `[project].workspace_dir`; without it the command fails with a config error.
- An absolute `dir` is allowed and may be on another filesystem (the move is copy-then-delete).
- The archive directory must not equal or contain the workspace root. That is a config error.
- The loader checks only that `dir` is a string and returns it as written. It does not expand `~`, resolve it
  or touch the filesystem. Callers expand `~` and resolve the path, and check the rule above, when they use it.

## Minimal example

```toml
[project]
repo_dir = "."

[source.work_item]
kind = "github"

[limits]
max_tickets_per_run = 1
```
