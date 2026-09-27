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

Excerpt from chunk A-3-4's mini-requirements doc, §7 (Detailed functional requirements):

### 7.1 Sequential mode (`spec_prism_flow/build/track_runner.py`)
`discover_phase_files(phases_dir: Path) -> list[Path]` — globs `*-leaf.md` under `phases_dir`, matched via Phase 01's `phase_file_name` regex, sorted by the numeric group.
`already_merged_phase_numbers(completion_log_path: Path) -> set[int]` — reads the completion log (Phase 10) and returns `{record.phase_number for record in records if record.pr_merged_at is not None}`. It returns an empty set, not an error, if the log does not exist yet.
`run_track(config, *, start_phase=None, stop_phase=None, dry_run=False, resume=False, strict=False) -> list[PhaseRunResult]` — processes the files returned by `discover_phase_files` in order. It skips phase numbers outside the optional `[start_phase, stop_phase]` inclusive bounds and phase numbers already in `already_merged_phase_numbers`. It calls Phase 11's `run_phase` for the rest, passing `strict` through unchanged. It does not catch Phase 06's `OrchestrationError`. The error propagates to the CLI layer with `phase_number` and `phase_name` annotations. This halts the whole run.

### 7.2 Parallel mode (`spec_prism_flow/build/parallel_runner.py`)
`eligible_leaves(graph, completion_log, phase_files) -> list[PhaseFile]` — a leaf is eligible iff its `pr_merged_at` is `None` and every dependency edge from it in `graph.json` points to a leaf whose `pr_merged_at` is not `None`.
`run_parallel(config, *, workers: int, dry_run=False, strict=False) -> list[PhaseRunResult]` — repeatedly computes `eligible_leaves` and submits up to `workers - (leaves currently running)` of them to a bounded worker pool (`concurrent.futures.ThreadPoolExecutor(max_workers=workers)` — subprocess-bound work, not CPU-bound). Each submitted leaf invokes Phase 11's `run_phase` independently, with the same `strict` value. As each leaf completes, re-poll eligibility. On an escalation for leaf L, do not submit any leaf that depends (transitively, via the graph) on L, but continue running/starting unrelated eligible leaves. The run ends when every leaf is either merged or blocked-by-escalation.

Concurrent completion-log writes across these leaves need no coordination here. Each `run_phase`'s final step goes through Phase 10's `completion_log_append` (`append_and_commit`). That function re-fetches the shared `base_branch` log and retries on a rejected force-with-lease push. It skips the append if a racing call's record already landed.

### 7.3 `build` CLI (`spec_prism_flow/cli.py` additions, `click`-based)
- `spec-prism-flow build run [--start N] [--stop N] [--dry-run] [--resume] [--strict]`
  - Reads `config.build.workers`.
  - `workers == 1` calls `run_track` (sequential mode).
  - `workers > 1` calls `run_parallel` (parallel mode). Concurrent completion-log writes across its leaves are conflict-safe per §7.2's `append_and_commit` retry loop, so no extra coordination is needed at the CLI layer.
  - `workers <= 0` raises a `click.UsageError` naming the invalid value. Phase 01's config schema does not constrain `workers` to be positive, so this dispatch is the sole enforcement point.
  - `--strict` is off by default (per root requirements §3/§11 #2). It is passed through unchanged to whichever of `run_track`/`run_parallel` is called, which in turn passes it to Phase 11's `run_phase`. This is the only way to enable the hard manual-test merge gate. No config-file equivalent exists.
  - `--start`/`--stop` apply to sequential mode only. Passing both in parallel mode raises a `click.UsageError`.
  - On an `OrchestrationError`, print the escalation (message + `next_command` hint if present) and exit with the error's exit code.
- `spec-prism-flow build status`
  - Reads `{config.plan.phase_dir}/*-leaf.md` (never a hardcoded `docs/phases`, per Phase 01's required, no-default `plan.phase_dir`) and the completion log.
  - Prints one row per phase: number, name, status, address-comments cycle count, escalation count.
  - Classifies status per phase from two sources, and introduces no new state file:
    - `merged`: the completion log has a record for that phase number with `pr_merged_at` set.
    - `escalated`: otherwise, the log has a record with `escalation_reason` set (Phase 11's best-effort partial write on `OrchestrationError`).
    - `in progress`: otherwise, a resume-state file exists at Phase 08's `resume_state_path(config, repo, branch_name(phase))` (`branch_name` from Phase 07). That file is written the moment `run_phase`'s `pr_create` first succeeds and is only removed on merge. So its mere presence without a completion-log record is exactly "started, not yet finished."
    - `pending`: otherwise.
  - Uses a plain-text table (`click.echo` with fixed-width formatting). No `rich.Table` dependency.

## Acceptance criteria
- `discover_phase_files` returns only files matching the `NN-name-leaf.md` pattern, sorted numerically (not lexicographically — `10-...` must sort after `09-...`, not before `02-...`).
- `already_merged_phase_numbers` returns `set()` (not an exception) when the completion log does not exist.
- `run_track` skips already-merged phases and phases outside `[start_phase, stop_phase]`, and propagates `OrchestrationError` uncaught, annotated with `phase_number`/`phase_name`.
- `eligible_leaves` correctly computes eligibility from `graph.json` edges and merge status on constructed fixtures, including a leaf with multiple dependencies where only some have merged (not yet eligible).
- `run_parallel` runs up to `workers` leaves concurrently. It re-polls eligibility as leaves complete. On one leaf's escalation, it continues running/starting leaves with no dependency path to the escalated leaf, while blocking only its (transitive) dependents.
- `workers=1` (sequential) and a single-node graph in parallel mode produce observably identical phase-run order for the same fixture corpus.
- `build run` selects sequential vs. parallel mode based on `config.build.workers`. It raises a `click.UsageError` naming the value for `workers <= 0`. It rejects `--start`/`--stop` in parallel mode with a `click.UsageError`. It exits with the raised error's `exit_code` on escalation.
- `build status` prints a correctly classified row per phase, including distinguishing `pending` from `in progress` via resume-state-file presence (per §7.3). No `rich`/`typer` imports appear anywhere in this phase's modules.
- `uv run pytest` passes for all three test files. `ruff check`/`ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_track_runner.py tests/build/test_parallel_runner.py tests/build/test_build_cli.py -v` and confirm all cases above pass.
- Against a fixture corpus of 12 phase files (numbered 01–12) plus a completion log with a few already merged, run `discover_phase_files`/`already_merged_phase_numbers` and confirm correct numeric sort and skip behavior.
- Construct a small fixture graph with two independent branches. Deliberately escalate one leaf (via a stubbed `run_phase`). Confirm `run_parallel` continues completing the unrelated branch rather than halting the whole run.
- Run `spec-prism-flow build run --dry-run` (with `workers=1`) against a fixture corpus. Confirm phases execute in order via the dry-run toolchain. Run `spec-prism-flow build status` afterward. Confirm the printed table reflects the dry run's outcome.
- Manually write a resume-state file for a phase with no completion-log entry (simulating a mid-flight run). Confirm `build status` reports it as `in progress` rather than `pending`. Remove the file. Confirm the same phase reverts to `pending`.
- Attempt `spec-prism-flow build run --start 3 --stop 5` with `config.build.workers=2` and confirm a `click.UsageError` is raised rather than silently ignoring `--start`/`--stop`.
- Run `spec-prism-flow build run` with `config.build.workers=0` and confirm a `click.UsageError` naming the invalid value is raised, rather than exiting 0 having run nothing or crashing with an uncaught `ValueError`.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 11 merged.
