# dotharness

Personal AI tooling for automating recurring engineering workflows with
configurable AI backends (Claude Code or opencode). The project is a single uv
package, `dotharness`, with four command-line tools.

The tools are being consolidated into this package. Each page below is a stub
until the issue that migrates the tool lands.

| Tool | Purpose | Page |
|---|---|---|
| `vibe_heal` | Fix static-analysis findings automatically | [vibe_heal](vibe_heal.md) |
| `pr_review` | PR automation and AI code review | [pr_review](pr_review.md) |
| `spec_prism_flow` | Spec-driven build flow | [spec_prism_flow](spec_prism_flow.md) |
| `sisyphus` | Issue-to-PR automation | [sisyphus](sisyphus.md) |

See [Contributing](contributing.md) for the branch rule and the
[Migration ledger](migration-ledger.md) for the progress of the migration.
