# sisyphus

[![CI](https://github.com/alexeieleusis/dotharness/actions/workflows/sisyphus-main.yml/badge.svg)](https://github.com/alexeieleusis/dotharness/actions/workflows/sisyphus-main.yml)
[![codecov](https://codecov.io/gh/alexeieleusis/dotharness/branch/main/graph/badge.svg)](https://codecov.io/gh/alexeieleusis/dotharness)

Autonomous development loop: tracker intake to PR open for review. A finished item is "PR open, handed to review"; after the PR opens, pr-review (and possibly vibe-heal) take over

This tool lives in the [alexeieleusis/dotharness](https://github.com/alexeieleusis/dotharness) monorepo under `tools/sisyphus/`. It is not (yet) published as a standalone repository or package.

- **Documentation**: <https://alexeieleusis.github.io/dotharness/sisyphus/>

## Quickstart

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), `git`, `gh` and an agent CLI (`claude` or `opencode`).

```bash
make install
```

Create a `.sisyphus.toml` for the dedicated clone Sisyphus works in:

```toml
[project]
repo_dir = "/path/to/dedicated/clone"
workspace_dir = "/path/to/workspace"
brief_path_template = "/path/to/workspace/{item}.brief.md"

[source.work_item]
kind = "github"
assignee = "your-login"

[agent]
backend = "claude"

[limits]
max_tickets_per_run = 1
```

Then run a single pass and inspect or continue stopped items:

```bash
uv run sisyphus run --config .sisyphus.toml --base main
uv run sisyphus status --config .sisyphus.toml
uv run sisyphus resume <work-item-ref> --config .sisyphus.toml
uv run sisyphus abandon <work-item-ref> --config .sisyphus.toml
```

To feed Sisyphus from local ticket files instead of a tracker, use the `file` kind. It picks the next
unstarted `<dir>/<ref>.md` ticket file and needs no `assignee`, API key or network:

```toml
[project]
repo_dir = "/path/to/dedicated/clone"
workspace_dir = "/path/to/workspace"
brief_path_template = "<workspace_dir>/<item-id>.brief.md"

[source.work_item]
kind = "file"
dir = "/path/to/tickets"
```

Sisyphus substitutes `<workspace_dir>` and `<item-id>` in `brief_path_template`. A producer prompt (`knowledge/sisyphus/linear-feed-prompt.md`) can export Linear tickets into `dir`. See
[usage](https://alexeieleusis.github.io/dotharness/sisyphus/usage/).

Sisyphus never pushes to the base branch: each item gets its own `sisyphus/<slug>-<8 hex>` branch and a PR.

> **Caveat:** the real `claude`/`opencode`, `gh`, Linear and `git push` paths are covered by tests with fakes only. They have not been exercised against the
> real services.

See the [documentation](https://alexeieleusis.github.io/dotharness/sisyphus/) for usage, configuration,
checkpoints and safety rails.

---

Repository initiated with [osprey-oss/cookiecutter-uv](https://github.com/osprey-oss/cookiecutter-uv).
