# Intake

`sisyphus.intake.run_intake(item, config, runner, record=None)` turns a work-item reference into a brief at
`[project].brief_path_template` (placeholders `<workspace_dir>` and `<item-id>`, where `<item-id>` is the
run-record encoding of the reference). It needs `[project].repo_dir` and `[project].workspace_dir`.

## Prompt and placeholders

The prompt text is `knowledge/sisyphus/intake-prompt.md`, resolved through `sisyphus.paths`. It has exactly two
placeholders, `<work-item reference>` and `<brief path>`. Both are substituted. It does not copy the drafting
prompt. A trailer line with the absolute path of `requirements-doc-drafting-prompt.md` (resolved through
`paths.py`) is appended. The agent fetches the item, grounds the brief in the repository, and writes only the
brief.

## Injected runner

`AgentRunner = Callable[[str], None]`: it receives the final prompt and must write the brief. Choosing the agent
backend is the caller's job. Tests use fakes. Nothing here launches an agent.

## Result and C1

The result is `IntakeResult(brief_path, skipped, open_decisions)` with `blocked` and `checkpoint` (`Checkpoint.C1` when
blocking decisions exist, else `None`). Blocking decisions are the unresolved top-level numbered entries under
the brief's "Open decisions log" heading. An entry starting with "resolved" or containing "status: resolved" is
not blocking. `parse_resolved_decisions(text)` returns those resolved entries and `split_resolved_decision(text)`
splits one into question and answer; the PR body (see [orchestrator](orchestrator.md)) uses them. The orchestrator decides what to do. It must never run `plan init` on a blocked brief. Intake
also sets checkpoint `C1` on the run record. It clears the checkpoint if it was `C1` and nothing blocks any more.

## Git-status boundary

`repo_unchanged(repo_dir, ignore=())` is a reusable context manager. It snapshots `git status` plus a content
hash of every non-clean file. It raises `BoundaryViolation` (an `IntakeError`) if the snapshot differs after the
body. `run_intake` wraps the runner call in the context manager. The code enforces the boundary, not the prompt.

Git-ignored files count, because the snapshot uses `--ignored`. A new `.venv/` or `uv.lock` that `uv run` leaves in
the repository therefore stops intake. The violation message (and the stop record detail) names the paths: the
first sentence, then `added: ...`, `removed: ...` and `changed: ...` groups (paths sorted within each group), at most
10 paths in total followed by `and N more`, and `HEAD changed` or `branch changed` when those moved. The prompt
tells the agent not to run `uv`, create virtualenvs, or write lockfiles or caches in the repository, and to do scratch
work outside it. That is advice only; the code guard is the enforcement.

There is no exemption list: no config key, no built-in list of tolerated paths. The intake stop is a `C5`. The fix is
to remove or revert the listed paths by hand and run `sisyphus resume`. Resuming runs intake again, and no
`--reset-retries` is needed.

## Run record and skipping

The stage is marked `intake` with `stage_scope`. It is in progress before the run and complete after it. A runner error or boundary
violation leaves it in progress. If the brief exists and a sidecar `<brief>.intake.json` (item plus SHA-256 of
the final prompt text) matches, the runner is skipped (NFR-4) and the brief is parsed again. Because the brief is parsed again, a human edit that
resolves the log clears `C1`. Changing the prompt file reruns intake and overwrites the brief.
