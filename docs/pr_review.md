# pr_review

Generic PR automation with configurable AI backends. `pr_review` is one of the tools in `dotharness`; it was migrated
from the standalone `tools/pr-review` tool (package `harness`) in I-3 (#165). See the
[migration ledger](migration-ledger.md) for module status.

The CLI name (`pr_review`) and the config file names (`.harness*.toml`) differ on purpose: the config files keep the old
prefix so existing setups keep working.

## Pages

- [Overview](pr_review/index.md): what the tool does and a quick start
- [Modules](pr_review/modules.md): the generated API reference and the command module layout

## Commands

- [`review-prs`](pr_review/commands/review-prs.md)
- [`focused-review`](pr_review/commands/focused-review.md)
- [`review-requested`](pr_review/commands/review-requested.md)
- [`self-review`](pr_review/commands/self-review.md)
- [`address-comments`](pr_review/commands/address-comments.md)
- [`auto-approve`](pr_review/commands/auto-approve.md)
- [`local-review`](pr_review/commands/local-review.md)
- [`ci-fix`](pr_review/commands/ci-fix.md)
- [`merge`](pr_review/commands/merge.md)
