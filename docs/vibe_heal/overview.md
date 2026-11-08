# Overview

AI-powered SonarQube issue remediation that automatically fixes your code quality problems using
Claude Code or opencode.

## Features

- **Branch cleanup**: Automatically fix all modified files in a branch before code review
- **Code deduplication**: AI-powered removal of duplicate code blocks
- **Branch review**: Report SonarQube issues scoped to changed lines and post inline GitHub PR comments
- **`sonar-project.properties` support**: Honors your existing scanner configuration file — only adds missing auth or host flags, and temporarily patches the project key/name for temp-project runs
- AI-powered issue fixing with **Claude Code** or **opencode**
- **Enriched AI prompts** with full rule documentation and code context
- Automatic git commits per fix with conventional commit format
- Smart issue ordering (reverse line order to avoid line number shifts)
- Support for both SonarQube old and new API formats
- Dry-run mode for testing without committing
- AI tool auto-detection (tries Claude Code first, then opencode)

## Commands

| Command | Description |
|---|---|
| `vibe_heal fix <file>` | Fix SonarQube issues in a single file |
| `vibe_heal dedupe <file>` | Remove code duplications from a single file |
| `vibe_heal cleanup` | Fix all modified files in the current branch |
| `vibe_heal dedupe-branch` | Remove duplications from all modified files in the current branch |
| `vibe_heal cleanup-pr` | Fix issues and duplications on lines changed by the current branch (PR scope) |
| `vibe_heal review` | Report issues on changed lines. Optionally post them to a GitHub PR |
| `vibe_heal review --baseline` | Refresh the real SonarQube project's analysis (for CI baseline maintenance) |
| `vibe_heal config` | Show current configuration |
| `vibe_heal version` | Show version information |

## Quick links

- [Branch Cleanup Guide](branch-cleanup-guide.md) — full guide for the `cleanup`, `dedupe-branch` and `cleanup-pr` commands
- [Review Guide](review-guide.md) — full guide for the `review` command and GitHub PR commenting
- [Architecture](architecture.md) — system design and module structure
- [API reference](api.md) — generated reference for `dotharness.vibe_heal`
- [`vibe_heal` entry page](../vibe_heal.md)
