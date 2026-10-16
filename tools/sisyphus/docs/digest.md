# Digest

`sisyphus.digest` writes one Markdown file per run at `[digest].path_template`. File only: there is no
notification command.

## Placeholders

`<date>` (UTC, `YYYY-MM-DD`) and `<workspace_dir>` (from `[project].workspace_dir`). A missing template, or
`<workspace_dir>` without a workspace, raises `DigestWriteError`.

## Sections

Always all six, always in this order. An empty section says `None`.

1. **Needs you**: a numbered list of every checkpoint C1..C8 reached, each phrased as an answerable question
   in the drafting prompt's open-decisions-log style. Questions come from `DigestReport.needs_you` and, for any
   run record whose `checkpoint` is set and not already covered, from its stop record
   (`stoprecord.py`) or a built-in question per checkpoint. Entries can carry `Action:` (what you must do) and
   ``Resume: `cmd` `` (`NeedsYou.action`, `NeedsYou.resume`). Items still waiting for you are listed as
   "Awaiting you"; an unexpected error is listed as `[error]`.
2. **Done**: PRs opened or updated and commits, each with the evidence strings the caller supplies
   (`DoneEntry.evidence`, rendered verbatim; no gate-record format is defined here), plus run records whose
   stage is `ci-fix` or `merge` and complete.
3. **Declined**: CI failures not auto-fixed with why (`DeclinedEntry`), plus `ci-fix` or `merge` records that
   did not finish.
4. **Deviations**: `DigestReport.deviations`.
5. **Limits hit**: `DigestReport.limits_hit` plus records at checkpoint C6.
6. **Cost and time**: per record, the stage reached and wall-clock since `started_at`; per `StageTiming`, seconds
   and tokens when the backend reported them.

## Inputs

`render_digest(records, report=None) -> str` and `write_digest(records, config, report=None, today=None) -> Path`.
`records` are `runrecord.RunRecord` objects. The digest reads only `item`, `stage`, `stage_status`,
`checkpoint`, `started_at`, `updated_at` and `retries` (retry keys that start with the stage name are shown for
`ci-fix` and `merge`). The stage names are `digest.CI_FIX_STAGE` (`ci-fix`) and `digest.MERGE_STAGE` (`merge`).
Only the stages reached are rendered, so a run that failed early still produces a full digest.

## Errors

If the file cannot be written (unwritable path, missing template) `write_digest` raises `DigestWriteError`. The
CLI is expected to print it on stderr and exit non-zero.
