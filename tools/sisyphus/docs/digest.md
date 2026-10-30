# Digest

`sisyphus.digest` writes one Markdown file per run at `[digest].path_template`. The output is only a file. There is no notification command.

## Placeholders

Two placeholders are available: `<date>` (UTC, `YYYY-MM-DD`) and `<workspace_dir>` (from `[project].workspace_dir`). A missing template, or
`<workspace_dir>` without a workspace, raises `DigestWriteError`.

## Sections

Always include all six, in this order. An empty section says `None`.

1. **Needs you**: a numbered list of every checkpoint C1..C8 reached, each phrased as an answerable question
   in the drafting prompt's open-decisions-log style. Questions come from `DigestReport.needs_you`. For any run record whose `checkpoint` is set and not already covered, they also come from its stop record (`stoprecord.py`) or from a built-in question for that checkpoint. Entries can carry `Action:` (what you must do) and
   ``Resume: `cmd` `` (`NeedsYou.action`, `NeedsYou.resume`). Items still waiting for you are listed as "Awaiting you". An unexpected error is listed as `[error]`.
2. **Done**: PRs opened or updated, and commits. Each has the evidence strings the caller supplies (`DoneEntry.evidence`, rendered verbatim). A finished item is listed as `<item>: draft PR #N opened`, and its evidence says the gate passed, failed (with the command, directory and exit code) or was not run. A failed gate adds a Deviations note and no "Needs you" entry. That is "draft PR open, handed to the human": there is no "Needs you" entry and no follow-up line for it.
3. **Declined**: entries with the reason (`DeclinedEntry`).
4. **Deviations**: `DigestReport.deviations`, plus one `Abandoned: REF ...` note for each record whose stop record has `kind` `abandoned`. Each note includes the time and the re-enable command. See `sisyphus abandon` in usage. Abandoned items are
   never listed under Needs you. A kept local branch adds `Branch kept: REF <branch> <reason>`. When no proof held, it includes `no deletion proof held: (1) ...; (2) ...; (3) ...`. If `-D` is refused on proof 3, the note says so. A deleted local branch is listed under Done with the proof used: `proof 1`, `proof 2`, or `proof 3`. A proof 3 squash merge deleted with `-D` gives the full 40-character head SHA, the PR number, and the hint `git fetch origin pull/<N>/head` to recover it. Each explicit retry reset adds `Retry reset: REF at stage S on <time>: key a 3->0, ...`
   (or `no counters to reset`), read from the run record's `resets` list, so it is shown on every later digest.
5. **Limits hit**: `DigestReport.limits_hit` plus records at checkpoint C6.
6. **Cost and time**: For each record, the stage reached and the wall-clock time since `started_at`. For each `StageTiming`, the seconds and the tokens, when the backend reported them.

## Inputs

`render_digest(records, report=None) -> str` and `write_digest(records, config, report=None, today=None) -> Path`.
`records` are `runrecord.RunRecord` objects. The digest reads only `item`, `stage`, `stage_status`,
`checkpoint`, `started_at`, `updated_at`, `path` and `resets`.
Only the stages reached are rendered, so a run that failed early still produces a full digest.

## Errors

If the file cannot be written (unwritable path, missing template), `write_digest` raises `DigestWriteError`. The
CLI is expected to print it on stderr and exit non-zero.
