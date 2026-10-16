# Intake

`sisyphus.intake.run_intake(item, config, runner, record=None)` turns a work-item reference into a brief at
`[project].brief_path_template` (placeholders `<workspace_dir>` and `<item-id>`, the latter being the
run-record encoding of the reference). It needs `[project].repo_dir` and `[project].workspace_dir`.

## Prompt and placeholders

The prompt text is `knowledge/sisyphus/intake-prompt.md`, resolved through `sisyphus.paths`. It has exactly two
placeholders, `<work-item reference>` and `<brief path>`; both are substituted. It does not copy the drafting
prompt. A trailer line with the absolute path of `requirements-doc-drafting-prompt.md` (resolved through
`paths.py`) is appended. The agent fetches the item, grounds the brief in the repository, and writes only the
brief.

## Injected runner

`AgentRunner = Callable[[str], None]`: it receives the final prompt and must write the brief. Choosing the agent
backend is the caller's job. Tests use fakes; nothing here launches an agent.

## Result and C1

`IntakeResult(brief_path, skipped, open_decisions)` with `blocked` and `checkpoint` (`Checkpoint.C1` when
blocking decisions exist, else `None`). Blocking decisions are the unresolved top-level numbered entries under
the brief's "Open decisions log" heading; an entry starting with "resolved" or containing "status: resolved" is
not blocking. The orchestrator decides what to do and must never run `plan init` on a blocked brief. Intake
also sets checkpoint `C1` on the run record (and clears it if it was `C1` and nothing blocks any more).

## Git-status boundary

`repo_unchanged(repo_dir, ignore=())` is a reusable context manager that snapshots `git status` plus a content
hash of every non-clean file, and raises `BoundaryViolation` (an `IntakeError`) if the snapshot differs after the
body. `run_intake` wraps the runner call in it; the code enforces it, not the prompt.

## Run record and skipping

The stage is marked `intake` in progress before and complete after (`stage_scope`); a runner error or boundary
violation leaves it in progress. If the brief exists and a sidecar `<brief>.intake.json` (item plus SHA-256 of
the final prompt text) matches, the runner is skipped (NFR-4) and the brief is parsed again, so a human edit that
resolves the log clears C1. Changing the prompt file reruns intake and overwrites the brief.
