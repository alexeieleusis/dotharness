# Configuration

Each target repository declares its Sisyphus settings in a `.sisyphus.toml` file.
`sisyphus.config.load_config(path)` loads and validates the file. It fails with a `ConfigError` that names the section and key when the file has any of these problems:

- unknown keys
- unsupported kinds
- a missing `dir` for `file`
- wrong value types
- non-positive limits
- mixed trackers

## Path rule

Path values (`repo_dir`, `workspace_dir`, `brief_path_template`, `[source.work_item]` `dir`,
`digest.path_template`, `[archive]` `dir` and the `[knowledge]` paths) are returned exactly as written.
Relative paths resolve against the invoking working directory, not the directory that holds the config file.
The loader never touches the filesystem for them. The one exception is `[archive] dir`, which commands resolve
against `[project].workspace_dir` (see [`[archive]`](#archive)). When validating `[refactor] pattern_set`, the loader lists the packaged `knowledge/patterns/`
through `stack.pattern_set_names()` (see [`[refactor]`](#refactor)). That is packaged data, not a user-supplied path value.

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
| `base_branch` | string | none (the repository default branch, else `main`) |

`base_branch` is the PR base branch, also the branch the intake clone starts from. Precedence: `sisyphus run --base <branch>`,
then `[project].base_branch`, then the repository default branch as the code host declares it, then `main` (the
final fallback, used when the host cannot be read). A repository whose default branch is `main` and that sets
no key behaves as before. A repository whose default is `develop` now gets `develop` with no configuration.

Set the key explicitly when the host default may not be your integration branch. The hazard is a stale
legacy `main` that is still the host default while the team works on another branch: Sisyphus would
target `main`. Set `base_branch` to the real branch. Also set it if you work on your own stacked branch (a branch whose PR targets
another unmerged branch).

The key is per tool. `pr_review` has `[repo].base_branch` and `vibe_heal` has `DOTHARNESS_BASE_BRANCH`; they are
not shared. For a repository that uses several tools, set the same short name (for example `develop`, not
`origin/develop`) in each. A mismatch is not detected.

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
| `remove_label_when_settled` | bool (github only, needs `pick_label`; rejected for `linear`, ignored for `file`) | depends on the host: on for `kind = "github"` when the host can remove a label, off when it cannot, else none |
| `dir` | string (file only, required for `file`) | none |

A `[source.linear]` or `[source.github]` table is rejected (as "two trackers" when `[source.work_item]` is
also present). `pick_state` with `kind = "github"` and `pick_label` with `kind = "linear"` are rejected as
mixed-tracker keys. `kind = "file"` accepts both, because the producer prompt reads them.

For `kind = "file"`, `dir` is required and names the directory of ticket files (`<dir>/<ref>.md`). Sisyphus
expands `~` in it. Sisyphus itself ignores `assignee`, `pick_state` and `pick_label` for `file`. These are parameters of the producer prompt
(`knowledge/sisyphus/linear-feed-prompt.md`). The prompt reads them from this table to choose the Linear ticket to export.
`assignee` and `pick_state` are required by the producer. `pick_label` is optional. `dir` is rejected for any other kind.

For `kind = "github"`, Sisyphus both reads and writes `pick_label`. With `remove_label_when_settled` on (the
default when the host can remove a label), it removes the label from the issue when the item settles. The item settles when the finish trigger has opened the
draft PR, or when you run `sisyphus abandon #<number>`. The label is removed even if the draft PR gate failed.
That gate failure stays in the digest and the PR. A removal failure is a digest note (finish) or one stderr
line (abandon), never a stop and never a changed exit code. Re-queuing is a manual relabel. `resume --again` does not restore the label, and items settled earlier are not back-filled. Set `remove_label_when_settled = false` to
keep the label. If the code host cannot remove a label from a work item, an absent key means off and nothing is
attempted or reported. An explicit `true` there prints one startup warning and is treated as `false`. Merged or closed PRs still do not exclude an issue from the pick.

### `[source.host]`

| Key | Type | Default |
|---|---|---|
| `token_cmd` | string | none (`gh` uses its own authentication) |
| `name` | string, the name of a code host | none (the reference host) |
| `options` | table | `{}` |

`name` selects the code host. It must be a non-empty string. Without it, Sisyphus uses the reference host.
`options` holds the settings of that host and is passed to it as given. `options` without `name` is a config error.
The loader checks only the types. It does not run `token_cmd`.

`token_cmd` runs once at startup, with no shell. Its stdout is the token. The reference host passes the token to
`gh` as `GH_TOKEN` only. When `token_cmd` is unset, no command runs and `gh`
uses its own authentication, as in earlier releases. If the command fails, the CLI stops with a `config error:` line (see
[Credentials and backends](usage.md#credentials-and-backends)). The host runs in `[project].repo_dir`.

#### Unsupported-capability policy

Sisyphus uses the `Repository`, `ChangeRequests` and `WorkItems` capabilities of the code host. A host may not
declare one of them. The table shows what Sisyphus does at each call site.

| Call site | Capability | Policy | Basis |
|---|---|---|---|
| `pick_items` candidate listing and linked/open-PR check (`github` kind) | WorkItems | stop | design (DEC-22) |
| Label removal (finish and `abandon`) | WorkItems | warn (digest note or one stderr line, never a stop) | design (DEC-22) |
| `find_pr`, `open_pr` in the open-PR stage | ChangeRequests | stop (a host error is a C8 stop) | inferred |
| `pr_state` in branch cleanup | ChangeRequests | degrade (a failed lookup keeps the branch with a note) | inferred |
| `status --check-prs` state lookup | ChangeRequests | warn (one `warning:` line to stderr, item shown) | inferred |
| default-branch lookup for intake | Repository | degrade (any failure returns `None`) | inferred |

For the first row, Sisyphus checks the capability at startup. With `kind = "github"` and no `WorkItems` capability,
the command stops before any host call with exit code 1 and this message: `config error: [source.work_item] kind =
"github" needs a code host with the WorkItems capability, which the configured host does not declare`.

#### Timeouts

The reference host applies the limits it declares: 30 seconds by default, and 10 seconds for the PR state lookup
and for label removal. Earlier, other Sisyphus calls to `gh` had a 3600 second limit. They now have 30 seconds.
This is a documented deviation from the design goal G1. A read timeout raises `CodeHostTimeoutError` with
`write=False`. A label removal timeout raises it with `write=True`.

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
| `max_leaves_per_run` | integer | 32 |
| `presentation_timeout_minutes` | integer | 5 |

`presentation_timeout_minutes` is the time limit, in whole minutes, for the call that rewrites a checkpoint stop
message (see [`[presentation]`](#presentation)). It is separate from `max_run_minutes`.

### `[presentation]`

Optional.

| Key | Type | Default |
|---|---|---|
| `present_stops` | boolean | `true` |

By default Sisyphus rewrites a checkpoint stop message through the agent backend. `present_stops = false` turns
that off, and so does `--raw` on `run` and `resume` (for one invocation). With either set, Sisyphus makes no
call, writes no `.stopped.md` and shows the raw text. A `.stopped.md` left by an earlier invocation is still
shown by `status` and the digest.

### Ignored sections

`[source.ci]` and `[merge]` are read by harness (pr-review) and ignored by Sisyphus. They are accepted
in the file and have no effect.

### `[digest]`

| Key | Type | Default |
|---|---|---|
| `path_template` | string | `"<workspace_dir>/digest-<date>.md"` |

### `[knowledge]`

| Key | Type | Default |
|---|---|---|
| `sizing_file` | string | none |
| `conventions_path` | string | none |

### `[refactor]`

Optional.

| Key | Type | Default |
|---|---|---|
| `enabled` | bool | `true` |
| `pattern_set` | string | none |

- `enabled = false` records the `refactor` stage as skipped. It shows as `stages_skipped` in the digest.
- `pattern_set` names a directory under `knowledge/patterns/`. The packaged sets are `generic`, `fastapi` and
  `react`.
- An unknown `pattern_set` name is a `ConfigError` at load time, and the message lists the valid names (the
  directories present under `knowledge/patterns/`, so the three packaged sets are the current ones, not a fixed
  list).
- With `pattern_set` set, all candidates (added or heavily modified source files) get that one set.
- The 50% modified-share threshold for existing files is a constant (`MODIFIED_SHARE_MIN` in `sisyphus/stack.py`),
  not a config key. There is no flag or gate for it.
- A `.sisyphus.toml` without `[refactor]` loads unchanged, with `enabled = true`.
- There is no per-run CLI override: the `[refactor]` config table is the only switch.

### `[archive]`

Optional.

| Key | Type | Default |
|---|---|---|
| `dir` | string | `archive` |

`sisyphus archive --config F` moves the files of settled items from `[project].workspace_dir` into the archive
directory (see [`sisyphus archive`](usage.md#sisyphus-archive)). A recorded gate failure does not keep an item from being moved. The layout is flat and keeps the original
names, for example `<archive>/_23147.json` and `<archive>/plan-147/`. The directory is created on first use.

- A relative `dir` resolves against `[project].workspace_dir`, not the invoking working directory, so the
  default is `<workspace_dir>/archive`. This is an exception to the Path rule above. A relative `dir` needs
  `[project].workspace_dir`. Without it the command fails with a config error.
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
