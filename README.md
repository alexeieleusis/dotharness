# .harness

Personal AI tooling, scripts, prompts, and knowledge files for automating recurring
engineering workflows with configurable AI backends (Claude Code or opencode).

## Tools

- [`tools/pr-review/`](tools/pr-review/README.md) — Configurable PR automation
  (`harness` CLI). It posts static-analysis feedback on open PRs, runs AI code
  review, and can address reviewer comments automatically, all driven by a per-repo
  `.harness.toml` config. See its [docs](tools/pr-review/docs/index.md) for the
  full command and configuration reference.

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

The root project `dotharness` registers four console scripts: `vibe_heal`,
`pr_review`, `spec_prism_flow` and `sisyphus`. They are stubs until the tools are
migrated (see [`docs/migration-ledger.md`](docs/migration-ledger.md)).

### Legacy install (until I-8)

The old tools stay installable until I-8. To use the current `harness` CLI:

```bash
uv tool install --editable ~/.harness/tools/pr-review/
harness init          # in each repo you want to automate
harness validate      # confirm config is correct
```

## Layout

- `src/dotharness/` — The `dotharness` Python package
- `tests/` — Tests for the package
- `docs/` — Documentation site (built with `uv run zensical build -s`), including
  the migration ledger and contributor notes
- `tools/` — The old, uv-managed tool packages, kept until I-8
- `vendor/` — Vendored dependencies and files (submodules, `containers/`)
- `knowledge` — Symlink to `src/dotharness/knowledge/`: Markdown knowledge files
  consumed by AI runners, model- and tool-agnostic (no assumptions about which LLM
  or coding-agent CLI reads them)
- `skills/` — Symlinks to the skills in `src/dotharness/skills/`: Claude Code Agent
  Skills (`skills/<name>/SKILL.md`). They use the Skill frontmatter schema
  (`name`/`description`/`version`) and are specific to that format, unlike the
  tool-agnostic files in `knowledge/`

## Contributing

Branch rule: I-1 targets `main`. I-2 to I-5 target `consolidation`. I-6 to I-8
target `main` after I-5 merges. PRs that touch `src/dotharness/` update the
[migration ledger](docs/migration-ledger.md). See
[`docs/contributing.md`](docs/contributing.md).

## License

[Apache-2.0](LICENSE)
