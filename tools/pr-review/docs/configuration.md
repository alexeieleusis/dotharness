# Configuration: .harness.toml

Every `harness run <command>` subcommand loads a `.harness.toml` file from the
current directory by default (override the path with `--config PATH` on the
`harness run` group, or pass a directory as the first argument to `harness
init` / `harness validate`). See the [index page](index.md#quick-start) for
the quick-start commands to scaffold and check one.

This file holds per-repo settings and can contain sensitive values (a
`gh_token_cmd`, arbitrary `env` entries, filesystem paths), so it is **not
meant to be committed** — the template written by `harness init` starts with
the comment `# dotharness configuration — DO NOT COMMIT this file` and
recommends adding `.harness.toml` to your global gitignore rather than the
repo's.

- `harness init [DIRECTORY]` writes a template `.harness.toml` into `DIRECTORY`
  (default: current directory). It refuses to overwrite an existing file.
- `harness validate [DIRECTORY] [--config PATH]` loads `DIRECTORY/.harness.toml`
  (or the file at `--config PATH` if given), parses it, and runs a handful of
  environment checks (see [Validation](#validation) below). It exits `0` if
  everything checks out and `1` (printing each problem to stderr) otherwise.

## Full schema

### `[harness]`

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | string | `"opencode"` | Which coding-agent CLI to shell out to. Must be `"opencode"` or `"claude"` — any other value raises a `ConfigError` at load time. `"opencode"` invokes `opencode run --dangerously-skip-permissions --pure <instructions>`; `"claude"` invokes `claude --dangerously-skip-permissions --disable-slash-commands -p <instructions>`. Both run with `cwd` set to `repo.working_dir`. |
| `gh_token_cmd` | string | `"gh auth token"` | Shell command whose stdout is used as the GitHub token. The token is exported to subprocesses as `GITHUB_TOKEN`. |
| `backend_timeout_seconds` | integer | `900` | Wall-clock timeout (seconds) for a single backend invocation before it is killed. |
| `knowledge_dir` | path | `"~/.harness/knowledge"` | Directory used for durable knowledge/notes the runners read/write across invocations. `~` is expanded. |
| `[harness.path_prepend]` | table (string → string) | `{}` (empty) | Arbitrary key names mapping to directories; all of the *values* are joined with `:` and prepended to `PATH` for every subprocess harness spawns (backend CLI, `git`, `gh`, pre-commands, etc.). Keys are just labels for readability (e.g. `java`, `node`) — only the values matter, and order follows the order they appear in the TOML table. |
| `[harness.env]` | table (string → string) | `{}` (empty) | Arbitrary `KEY = "value"` pairs merged into the environment of every subprocess harness spawns, overriding any inherited environment variable of the same name. |
| `review_knowledge_file` | path or unset | `None` | Optional path to a markdown file with extra review guidance; `~` is expanded. When unset, no extra file is loaded. |

### `[repo]`

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | string | **required** | Repo identifier, typically `"org/repo"`. Loading fails with `ConfigError: repo.name is required` if missing/empty. Also used to derive `repo_slug` (`name` with `/` replaced by `-`) for state files and schedule labels. |
| `working_dir` | path | **required** | Local filesystem checkout of the repo. Loading fails with `ConfigError: repo.working_dir is required` if missing/empty. `~` is expanded. |
| `opencode_dir` | path or unset | `None` | Optional directory passed to the `opencode` backend as `--dir` instead of the repo root. If set, it **must be inside `working_dir`** — the loader calls `opencode_dir.relative_to(working_dir)` and raises `ConfigError: repo.opencode_dir '<path>' must be inside repo.working_dir '<path>'` if it isn't a subpath. `~` is expanded before the check. |

### `[[repo.subdir]]`

An array of tables describing sub-projects inside a monorepo. Each subdir can
be tied to its own `sonar-project.properties` file so that
`get_vibe_heal_context` can look up a matching vibe_heal/SonarQube review
(`~/.vibe-heal/reviews/<sonar.projectKey>/<branch>/review.md`) and fold it into
the review context for that part of the repo. Subdirs can also carry their own
pre-commands (e.g. `npm ci`, `mvn compile`), a coverage flag, and a timeout,
which is useful when different sub-projects need different setup before a
review or pre-commit step.

| Field | Type | Default | Description |
|---|---|---|---|
| `path` | string | **required** | Path of the sub-project, relative to `repo.working_dir` (e.g. `"."` for the repo root, or `"services/api"`). Loading fails with `ConfigError: repo.subdir[].path is required` if missing. |
| `pre_commands` | list | `[]` | Commands to run before operating on this subdir. Each entry is either a plain string (shorthand for `{ cmd = "...", critical = false }`) or a table `{ cmd = "...", critical = true/false }`. See below for the two forms. |
| `coverage` | boolean | `false` | Whether this subdir participates in coverage collection. |
| `timeout` | integer | `300` | Timeout (seconds) for running this subdir's pre-commands. |

`pre_commands` entries may take either of two forms:

- **Plain string** — `"npm ci"` becomes `PreCommand(cmd="npm ci", critical=False)`.
- **Table** — `{ cmd = "npm ci", critical = true }` becomes `PreCommand(cmd="npm ci", critical=True)`. `critical` defaults to `false` if omitted from the table form too. A `critical` pre-command failing is treated as fatal for that subdir; a non-critical one is not.

### `[vibe_heal]`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Turns on vibe_heal/SonarQube-style review integration. When enabled, `review-prs` invokes vibe_heal to produce per-PR reviews, and other runners can fold matching review output (looked up via each subdir's `sonar-project.properties` → `sonar.projectKey`) into the review context. |
| `python` | string | `""` (unset) | Path to the Python interpreter (typically inside vibe_heal's own virtualenv) used to invoke vibe_heal. Only checked/required in practice when `enabled = true`. |
| `authors` | string or list of strings | `"*"` | Which PR authors vibe_heal should apply to in `review-prs`'s batch mode. `"*"` means all authors; otherwise a list of GitHub usernames. Ignored when `review-prs` is run with `--pr`. |
| `vibe_heal_timeout` | integer | `600` | Timeout (seconds) for the per-PR `vibe_heal review` run. |
| `vibe_heal_post_timeout` | integer | `120` | Timeout (seconds) for posting/finalizing vibe_heal output (e.g. writing results, comments) after the run itself completes. |
| `min_reanalysis_interval_hours` | number | `24.0` | Minimum time that must pass since a PR's last successful review before `review-prs`'s batch mode will re-analyze it, even if the head SHA has changed in the meantime (e.g. from rapid pushes). Ignored when `review-prs` is run with `--pr`. |
| `prune_projects_enabled` | boolean | `false` | Turns on a `vibe_heal prune-projects --yes` step, run in every `repo.subdir` at the start of every `review-prs` invocation, before baseline analysis. Deletes stale temp SonarQube projects (left behind by failed/interrupted vibe_heal runs) **without the CLI's confirmation prompt**. Off by default since it's a destructive, no-confirmation delete. |
| `prune_older_than_minutes` | integer | `60` | Passed as `vibe_heal prune-projects --older-than`; only temp projects with zero finished analyses older than this are eligible for deletion. Ignored if `prune_projects_enabled` is `false`. |
| `prune_projects_timeout` | integer | `120` | Timeout (seconds) for each subdir's `vibe_heal prune-projects` invocation. A timeout or failure is logged and does not block baseline analysis or PR review. |

### `[focused_review]`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `false` | Turns on the `focused-review` command. Independent of `vibe_heal.enabled` — not linked to it. |
| `vibe_types_repo` | path | `"~/.harness/vendor/vibe-types"` | Local git checkout of `jpablo/vibe-types`, used to resolve knowledge-file content (via `git show <commit>:<path>`) for SonarQube comments that cite it. `~` is expanded. |

### `[address_comments]`

| Field | Type | Default | Description |
|---|---|---|---|
| `trusted_commenters` | `"*"` or list of strings | `"*"` | Restricts which comment authors `address-comments` will act on at all; `"*"` (default) considers everyone. |

An inline comment thread carrying a `focused-review`-bot reply (marked `[focused-review-bot]`) is always held back until the harness's own `gh` account has left a `+1` reaction on that specific reply — checked live on every run, with no config flag to change this. Once approved, the thread is addressed using that reply's own content as the actual "comment to address" (not the terse original finding it responded to) — see [`address-comments`](commands/address-comments.md#notes) for details.

## Full example

```toml
# dotharness configuration — DO NOT COMMIT this file
# Add .harness.toml to your global gitignore

[harness]
backend = "opencode"               # "opencode" or "claude"
gh_token_cmd = "gh auth token"
backend_timeout_seconds = 900
knowledge_dir = "~/.harness/knowledge"

# [harness.path_prepend]
# java = "/Users/you/.sdkman/candidates/java/current/bin"
# node = "/Users/you/.nvm/versions/node/v22.20.0/bin"

# [harness.env]
# JAVA_HOME = "/Users/you/.sdkman/candidates/java/current"

# review_knowledge_file = "/path/to/review-guide.md"

[repo]
name = "org/repo"
working_dir = "/path/to/repo"
# opencode_dir must live inside working_dir if set
# opencode_dir = "/path/to/repo/apps/backend"

[vibe_heal]
enabled = false
# python = "/path/to/vibe-heal/.venv/bin/python3"
# authors = "*"
# vibe_heal_timeout = 600
# vibe_heal_post_timeout = 120
# min_reanalysis_interval_hours = 24.0
# prune_projects_enabled = false
# prune_older_than_minutes = 60
# prune_projects_timeout = 120

[focused_review]
enabled = false
# vibe_types_repo = "~/.harness/vendor/vibe-types"

[address_comments]
# trusted_commenters = "*"

# [[repo.subdir]]
# path = "."
# pre_commands = []
# coverage = false
# timeout = 300

# A fuller monorepo example for [[repo.subdir]] with both pre_commands forms:
# [[repo.subdir]]
# path = "services/api"
# pre_commands = [
#   "npm ci",
#   { cmd = "npm run build", critical = true },
# ]
# coverage = true
# timeout = 600
```

## Validation

`harness validate` loads the config and then checks, accumulating every error before exiting:

- `repo.working_dir` exists on disk.
- `harness.knowledge_dir` exists on disk.
- The backend binary is on `PATH` — `opencode` if `harness.backend =
  "opencode"`, otherwise `claude`.
- `gh` is on `PATH`.
- If `vibe_heal.enabled` is `true` and `vibe_heal.python` is set, that path
  exists on disk.

Any parse-time problem (invalid `backend`, missing `repo.name`/`repo.working_dir`,
an `opencode_dir` outside `working_dir`) is reported immediately and exits `1`
before these checks even run. If all checks pass, it prints
`Config valid: <repo.name> backend=<backend>` and exits `0`; if any check
fails, each failure is printed to stderr and it exits `1`.
