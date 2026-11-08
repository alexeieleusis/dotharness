# spec_prism_flow

Specification-driven planning: turns a rough idea into agent-sized phase files. `spec_prism_flow` is one of the
tools in `dotharness`. It was migrated from the standalone `tools/spec-prism-flow` tool in I-4 (#166). Only the
plan side (`spec_prism_flow plan ...`) is migrated. The build side is not migrated. See the
[migration ledger](migration-ledger.md) for module status.

`plan` accepts the `[agent] backend` config key but does not use it. A malformed `build.commands`, or
`review.enabled = true` without `harness_config`, no longer fails in `spec_prism_flow`.

## Pages

- [Overview](spec_prism_flow/overview.md): install, the `plan` commands and configuration notes
- [Lifecycle cheat sheet](spec_prism_flow/lifecycle-cheat-sheet.md): the idea-to-phase-corpus flow
- [Modules](spec_prism_flow/modules.md): the generated API reference
- [Requirements](spec_prism_flow/requirements.md): the vision and requirements document
