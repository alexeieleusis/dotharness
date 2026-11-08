# dotharness

Personal AI tooling for automating recurring engineering workflows with
configurable AI backends (Claude Code or opencode). It is one uv project,
`dotharness`, with four console scripts: `vibe_heal`, `pr_review`,
`spec_prism_flow` and `sisyphus`.

## Tools

- [`vibe_heal`](docs/vibe_heal.md)
- [`pr_review`](docs/pr_review.md) — Configurable PR automation: static-analysis
  feedback, AI code review, and automatic handling of reviewer comments. The CLI
  name `pr_review` differs from the `.harness*.toml` config file names on
  purpose.
- [`spec_prism_flow`](docs/spec_prism_flow.md)
- [`sisyphus`](docs/sisyphus.md)

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (installs Python ≥3.11 automatically)
- [`gh`](https://cli.github.com/) CLI, authenticated (`gh auth login`)
- An AI backend CLI — [Claude Code](https://claude.com/claude-code) or
  [opencode](https://opencode.ai/)

## Setup

```bash
git clone --recurse-submodules <repo-url> ~/.harness
cd ~/.harness
uv sync               # create the environment
uv run pytest         # confirm it works
uv tool install --editable ~/.harness
```

All four tools are migrated into `dotharness` (see
[`docs/migration-ledger.md`](docs/migration-ledger.md)).

## Layout

- `src/dotharness/` — The `dotharness` Python package, including
  `src/dotharness/knowledge/`: the packaged Markdown knowledge files consumed by AI
  runners, model- and tool-agnostic
- `tests/` — Tests for the package
- `docs/` — Documentation site (built with `uv run zensical build -s`), including
  the migration ledger and contributor notes
- `vendor/` — Vendored dependencies and files (submodules, `containers/`)
- `skills/` — Symlinks to the skills in `src/dotharness/skills/`: Claude Code Agent
  Skills (`skills/<name>/SKILL.md`). They use the Skill frontmatter schema
  (`name`/`description`/`version`) and are specific to that format

## Contributing

PRs that touch `src/dotharness/` update the
[migration ledger](docs/migration-ledger.md). See
[`docs/contributing.md`](docs/contributing.md).

## License

[Apache-2.0](LICENSE)
