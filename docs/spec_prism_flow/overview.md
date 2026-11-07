# spec_prism_flow overview

Specification-driven planning: turns a rough idea into agent-sized phase files. Only the plan side is
migrated into `dotharness`. The build side (`build run`, `build status`) is not migrated.

## Install and run

`spec_prism_flow` is a console script of the `dotharness` package:

```
uv run spec_prism_flow plan init BRIEF.md
uv run spec_prism_flow plan draft-overview
uv run spec_prism_flow plan draft-requirements
uv run spec_prism_flow plan decompose
uv run spec_prism_flow plan draft-phases
uv run spec_prism_flow plan review
```

Every `plan` command takes `--config PATH` (default `./.spec-prism-flow.toml`).

## Configuration notes

- The `[agent] backend` key is accepted but unused by `plan`.
- A malformed `build.commands`, or `review.enabled = true` without `harness_config`, no longer fails
  in `spec_prism_flow`. The `[build]`, `[review]` and `[vibe_heal]` tables are tolerated and ignored.

## Pages

- [Lifecycle cheat sheet](lifecycle-cheat-sheet.md): the idea-to-phase-corpus flow, step by step
- [Modules](modules.md): the generated API reference
- [Requirements](requirements.md): the vision and requirements document (Feature B is marked not migrated)
- [Migration ledger](../migration-ledger.md): behavior differences from the click-based original
