# API Reference

Reference for the `dotharness.vibe_heal` package, generated from docstrings.

## Core Modules

### CLI Interface
::: dotharness.vibe_heal.cli

### Orchestrator
::: dotharness.vibe_heal.orchestrator

## Configuration
::: dotharness.vibe_heal.config

## SonarQube Integration
::: dotharness.vibe_heal.sonarqube

## Issue Processing
::: dotharness.vibe_heal.processor

## AI Tools
::: dotharness.vibe_heal.ai_tools

## Git Operations
::: dotharness.vibe_heal.git

## PR-Scoped Cleanup

`vibe_heal cleanup-pr` fixes SonarQube issues and duplications that fall on lines changed relative to the base branch. It does not replace `cleanup` / `dedupe-branch`, does no GitHub interaction (run `review --post` afterwards) and does not push.

### CLI Flags

| Flag | Default | Behavior |
|---|---|---|
| `--base-branch`, `-b` | `origin/main` | Base to diff against; plain ref, no `gh` auto-detection |
| `--max-iterations`, `-i` | `10` | Maximum analyze -> fix rounds for the whole branch |
| `--pattern`, `-p` | none | Glob filters, same as `cleanup` (`Path.match`) |
| `--min-severity` | none | Passed to `IssueProcessor`; applies to issues only |
| `--dry-run` | off | Analysis and scoping only; no fix commits |
| `--ai-tool` | none | Same as `cleanup` |
| `--env-file` | none | Same as `cleanup` |
| `--verbose`, `-v` | off | Same as `cleanup` |
| `--include-main-duplications` | off | Also handle duplications from the base branch (runs a baseline scan) |

### Workflow

```txt
preconditions (repo, base branch, AI tool, clean tree) -> git fetch -> HEAD on top of base
  -> select files -> [baseline scan of base ref, flag only] -> create temp project
  -> [main-duplication phase, flag only] -> iteration loop -> delete temp project
```

Each loop round analyzes the full repo into the temporary project, recomputes the diff, then fixes per file: in-scope duplications first, then in-scope issues. Issues are kept only if `IssueLineFilter` keeps them (changed lines plus a 3-line trailing window); duplications only when the target block intersects changed lines. The baseline scan overwrites the real project's analysis on the SonarQube server, even with `--dry-run`. There is no revert logic: earlier commits are kept when a later step fails.

### Commit Formats

- Issue: `fix: [SQ-RULE] message`
- Duplication: `refactor: [duplication] remove duplicate code at line X`
- Main duplication: `refactor: [duplication] extract shared code from removed main duplication at line X`

### Shared-Plumbing Hooks

`VibeHealOrchestrator.fix_file(issue_filter=...)` and `DeduplicationOrchestrator.dedupe_file(group_filter=...)` accept optional predicates (default `None`) so `cleanup-pr` can scope the existing fixers.

### Result Models

`CleanupPrResult`: `success`, `files_processed: list[FileCleanupPrResult]`, `temp_project`, `analysis_result`, `total_issues_fixed`, `total_duplications_fixed`, `total_main_duplications_fixed`, `external_files_touched: list[Path]`, `error_message`.

`FileCleanupPrResult`: `file_path`, `issues_fixed`, `issues_out_of_scope`, `duplications_fixed`, `duplications_out_of_scope`, `main_duplications_fixed`, `main_duplications_skipped`, `success`, `error_message`.

::: dotharness.vibe_heal.cleanup_pr
