# Phase 12 — Track runner (sequential + parallel) & `build` CLI

*Purpose: drive the whole phase corpus to completion, sequentially or in parallel, and report where it stands.*

## Scope
- spec_prism_flow/build/track_runner.py
- spec_prism_flow/build/parallel_runner.py
- spec_prism_flow/cli.py (additions: `build run`, `build status`)
- tests/build/test_track_runner.py
- tests/build/test_parallel_runner.py
- tests/build/test_build_cli.py

## Requirements

Excerpt, chunk A-3-4's mini-requirements doc §7 (Detailed functional requirements), quoted verbatim:

### 7.1 Sequential mode (`spec_prism_flow/build/track_runner.py`)
`discover_phase_files(phases_dir: Path) -> list[Path]` — globs `*-leaf.md` under `phases_dir`, matched via Phase 01's `phase_file_name` regex, sorted by the numeric group.
`already_merged_phase_numbers(completion_log_path: Path) -> set[int]` — reads the completion log (Phase 10), returns `{record.phase_number for record in records if record.pr_merged_at is not None}`. Empty set, not an error, if the log doesn't exist yet.
`run_track(config, *, start_phase=None, stop_phase=None, dry_run=False, resume=False) -> list[PhaseRunResult]` — loops `discover_phase_files` in order, skips numbers outside the optional `[start_phase, stop_phase]` inclusive bounds and numbers already in `already_merged_phase_numbers`, calls Phase 11's `run_phase` for the rest. Does not catch Phase 06's `OrchestrationError` — it propagates to the CLI layer, annotated with `phase_number`/`phase_name` first, which halts the whole run.

### 7.2 Parallel mode (`spec_prism_flow/build/parallel_runner.py`)
`eligible_leaves(graph, completion_log, phase_files) -> list[PhaseFile]` — a leaf is eligible iff its `pr_merged_at` is `None` and every dependency edge from it in `graph.json` points to a leaf whose `pr_merged_at` is not `None`.
`run_parallel(config, *, workers: int, dry_run=False) -> list[PhaseRunResult]` — repeatedly: compute `eligible_leaves`, submit up to `workers - (leaves currently running)` of them to a bounded worker pool (`concurrent.futures.ThreadPoolExecutor(max_workers=workers)` — subprocess-bound work, not CPU-bound), each invoking Phase 11's `run_phase` independently; as each completes, re-poll eligibility; on an escalation for leaf L, do not submit any leaf that depends (transitively, via the graph) on L, but continue running/starting unrelated eligible leaves; the run ends when no leaves remain either merged or blocked-by-escalation.

### 7.3 `build` CLI (`spec_prism_flow/cli.py` additions, `click`-based)
- `spec-prism-flow build run [--start N] [--stop N] [--dry-run] [--resume]` — reads `config.build.workers`; `workers == 1` calls `run_track` (sequential), `workers > 1` calls `run_parallel` (parallel mode). `--start`/`--stop` apply to sequential mode only (passing both together in parallel mode is a `click.UsageError`). On an `OrchestrationError`, print the escalation (message + `next_command` hint if present) and exit with the error's exit code.
- `spec-prism-flow build status` — reads `docs/phases/*-leaf.md` + the completion log, prints one row per phase: number, name, status (`pending`/`in progress`/`escalated`/`merged`), address-comments cycle count, escalation count. Plain-text table (`click.echo` with fixed-width formatting) — no `rich.Table` dependency.

## Acceptance criteria
- `discover_phase_files` returns only files matching the `NN-name-leaf.md` pattern, sorted numerically (not lexicographically — `10-...` must sort after `09-...`, not before `02-...`).
- `already_merged_phase_numbers` returns `set()` (not an exception) when the completion log doesn't exist.
- `run_track` skips already-merged phases and phases outside `[start_phase, stop_phase]`, and propagates `OrchestrationError` uncaught, annotated with `phase_number`/`phase_name`.
- `eligible_leaves` correctly computes eligibility from `graph.json` edges and merge status on constructed fixtures, including a leaf with multiple dependencies where only some have merged (not yet eligible).
- `run_parallel` runs up to `workers` leaves concurrently, re-polls eligibility as leaves complete, and — on one leaf's escalation — continues running/starting leaves with no dependency path to the escalated leaf while blocking only its (transitive) dependents.
- `workers=1` (sequential) and a single-node graph in parallel mode produce observably identical phase-run order for the same fixture corpus.
- `build run` selects sequential vs. parallel mode based on `config.build.workers`, rejects `--start`/`--stop` in parallel mode with a `click.UsageError`, and exits with the raised error's `exit_code` on escalation.
- `build status` prints a correctly classified row per phase with no `rich`/`typer` imports anywhere in this phase's modules.
- `uv run pytest` passes for all three test files; `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_track_runner.py tests/build/test_parallel_runner.py tests/build/test_build_cli.py -v` and confirm all cases above pass.
- Against a fixture corpus of 12 phase files (numbered 01–12) plus a completion log with a few already merged, run `discover_phase_files`/`already_merged_phase_numbers` and confirm correct numeric sort and skip behavior.
- Construct a small fixture graph with two independent branches, deliberately escalate one leaf (via a stubbed `run_phase`), and confirm `run_parallel` continues completing the unrelated branch rather than halting the whole run.
- Run `spec-prism-flow build run --dry-run` (with `workers=1`) against a fixture corpus and confirm phases execute in order via the dry-run toolchain; run `spec-prism-flow build status` afterward and confirm the printed table reflects the dry run's outcome.
- Attempt `spec-prism-flow build run --start 3 --stop 5` with `config.build.workers=2` and confirm a `click.UsageError` is raised rather than silently ignoring `--start`/`--stop`.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 11 merged.
