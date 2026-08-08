# .harness

Personal AI tooling, scripts, prompts, and knowledge files for automating recurring
engineering workflows with configurable AI backends (Claude Code or opencode).

## Tools

- [`tools/pr-review/`](tools/pr-review/README.md) — Configurable PR automation
  (`harness` CLI). Sweeps open PRs to post static-analysis feedback, runs AI code
  review, and can address reviewer comments automatically, all driven by a per-repo
  `.harness.toml` config. See its [docs](tools/pr-review/docs/index.md) for the
  full command and configuration reference.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) (Python ≥3.11 is installed automatically by `uv`)
- [`gh`](https://cli.github.com/) CLI, authenticated (`gh auth login`)
- An AI backend CLI — [Claude Code](https://claude.com/claude-code) or
  [opencode](https://opencode.ai/)

## Setup

```bash
uv tool install --editable ~/.harness/tools/pr-review/
harness init          # in each repo you want to automate
harness validate      # confirm config is correct
```

## Layout

- `tools/` — Installable, uv-managed Python packages
- `knowledge/` — Markdown knowledge files consumed by AI runners; model- and
  tool-agnostic (no assumptions about which LLM or coding-agent CLI reads them)

## License

[Apache-2.0](LICENSE)
