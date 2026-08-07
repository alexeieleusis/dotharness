# dotharness-pr-review

[![CI](https://github.com/alexeieleusis/dotharness/actions/workflows/main.yml/badge.svg)](https://github.com/alexeieleusis/dotharness/actions/workflows/main.yml)
[![codecov](https://codecov.io/gh/alexeieleusis/dotharness/branch/main/graph/badge.svg)](https://codecov.io/gh/alexeieleusis/dotharness)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](../../LICENSE)

Generic PR automation with configurable AI backends (Claude Code or opencode).

Sweeps every open PR in a repo to post SonarQube/vibe_heal-style static-analysis
feedback, produces AI code reviews (on request or when review is requested from
you), and can read unresolved review comments and push the smallest fix. Behavior
per repo is driven entirely by a `.harness.toml` config file — see
[docs/configuration.md](docs/configuration.md) for the full schema and
[docs/commands/index.md](docs/commands/index.md) for what each command does.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (Python ≥3.11 is installed automatically by `uv`)
- [`gh`](https://cli.github.com/) CLI, authenticated (`gh auth login`)
- An AI backend CLI — [Claude Code](https://claude.com/claude-code) or
  [opencode](https://opencode.ai/)

## Setup

Clone the repo and install the CLI (editable, so it tracks your checkout):

```bash
git clone https://github.com/alexeieleusis/dotharness.git
uv tool install --editable dotharness/tools/pr-review
harness init          # scaffold ./.harness.toml in the repo you want to automate
harness validate      # confirm config is correct
```

(If you're already working inside this monorepo, `uv tool install --editable
~/.harness/tools/pr-review/` works the same way.)

## Usage

```bash
harness --help
harness run review-prs   # or any other command — see docs/commands/
harness run all          # run every command in sequence
```

## Documentation

- [docs/index.md](docs/index.md) — quick start
- [docs/configuration.md](docs/configuration.md) — `.harness.toml` schema
- [docs/commands/](docs/commands/) — one page per command
  (`review-prs`, `focused-review`, `review-requested`, `self-review`,
  `address-comments`)
- [docs/modules.md](docs/modules.md) — generated API reference

Build and browse them locally with `make docs`.

## Development

This is a [uv](https://docs.astral.sh/uv/)-managed Python project targeting
Python >=3.11 (CI runs 3.11–3.13).

```bash
make install   # uv sync + pre-commit install
make check     # lint (pre-commit/ruff), type-check (ty), deptry
make test      # pytest with coverage
make docs      # serve the docs locally
```

## License

[Apache-2.0](LICENSE)
