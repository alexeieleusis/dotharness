# Phase 10 — Manual test checklist & completion log

*Purpose: gate a phase's merge on an explicit human manual-test verdict, and record the outcome in an append-only completion log.*

## Scope
- spec_prism_flow/build/manual_test.py
- spec_prism_flow/build/completion_log.py
- tests/build/test_manual_test.py
- tests/build/test_completion_log.py

## Requirements

Excerpted from §7 (Detailed functional requirements) of chunk A-3-3-3's mini-requirements doc, restructured for clarity:

### 7.1 `spec_prism_flow/build/manual_test.py`
`class ManualTestOutcome: passed: bool; retry: bool; notes: str | None` (frozen dataclass). `prompt(phase_number: int, phase_name: str, checklist: list[str], *, strict: bool = False) -> ManualTestOutcome`:
- Empty `checklist` → prints a "no manual test checklist — skipping" notice, returns `ManualTestOutcome(passed=True, retry=False, notes=None)`.
- Otherwise prints the checklist (plain `click.echo`-formatted list — house style, no `rich` dependency) and prompts `click.confirm("Did every item pass?", default=False)`.
- Pass → `ManualTestOutcome(passed=True, retry=False, notes=None)`.
- Fail → prompts for notes (`click.prompt`, default `""`), then `click.confirm("Spend one more review cycle and retry, instead of escalating now?", default=False)`.
  - Retry chosen → `ManualTestOutcome(passed=False, retry=True, notes=notes or None)`. Caller decrements the retry budget (Phase 06's `RetryBudget`) and loops.
  - Retry declined, `strict=True` → `raise ManualTestFailed(notes or None)` (Phase 06's error type, unchanged signature).
  - Retry declined, `strict=False` (default) → `ManualTestOutcome(passed=False, retry=False, notes=notes or None)`. Caller records this as the terminal, non-blocking result and proceeds toward merge.

### 7.2 `spec_prism_flow/build/completion_log.py`
`class CompletionRecord` (frozen dataclass, single-track): `phase_number: int`, `phase_name: str`, `pr_number: int | None`, `pr_url: str | None`, `pr_opened_at: datetime | None`, `pr_merged_at: datetime | None`, `manual_test_first_try_pass: bool | None`, `escalation_reason: str | None`, `address_comments_cycles: int = 0`, `pr_diff_files: int = 0`, `pr_diff_lines_added: int = 0`, `pr_diff_lines_removed: int = 0`, `human_escalations: int = 0`.

- Non-defaulted fields precede all defaulted ones, as Python's dataclass requires.
- The class also exposes the property `pr_open_to_merge_seconds`.
- No `track`, `sonar_issues_*`, or `lensflow_attributable_issues` fields — dropped entirely, not kept-but-unused.

`load_all(log_json: Path) -> list[CompletionRecord]` — empty list if the file doesn't exist.

`append_and_commit(clone: Path, log_json: Path, log_md: Path, record: CompletionRecord, *, base_branch: str, max_conflict_retries: int = 5) -> None`

Safe to call concurrently from Phase 12's parallel mode, where multiple leaves finish around the same time and each appends its own record to the same shared `base_branch` log, with no external lock.

On each attempt, up to `max_conflict_retries` times:

1. Call `git_ops.fetch_resync`, then `load_all`.
2. Skip the append if a record with this `record.phase_number` is already present (a prior attempt's push landed even though this call later saw it as a lease rejection).
3. Otherwise, append the record and write `log_json` atomically: serialize with `indent=2` to a temp file in the same directory, then `os.replace` onto `log_json`, so a crash or kill mid-write can never leave `log_json` truncated/invalid for a later `load_all` to choke on.
4. Regenerate `log_md` (single-track table: Phase | PR | Address cycles | Open→merge | Diff | Escalations | Manual test).
5. Call `git_ops.commit_all`.
6. Call `git_ops.push_branch` (force-with-lease, per Phase 07's `git_ops`) directly against `base_branch`. Skip the push entirely if `commit_all` reports no changes — this is also how the already-present-record case above resolves: nothing new to commit, so nothing to push.

A `push_branch` failure means the lease was stale — another concurrent call's push landed first. Loop back to `fetch_resync` and retry against the new tip. After `max_conflict_retries` consecutive failures, let the last exception propagate uncaught.

## Acceptance criteria
- `prompt` returns exactly the documented `ManualTestOutcome` for each of the four branches (empty checklist, pass, fail-then-retry, fail-then-decline). It raises `ManualTestFailed` only in the fail-then-decline-with-`strict=True` case.
- With `strict=False` (default), a fail-then-decline never raises — it returns a failed, non-retrying, non-escalating outcome.
- `CompletionRecord` contains exactly the fields listed above — no `track`, `sonar_issues_*`, or `lensflow_attributable_issues` fields anywhere in the dataclass or its serialization.
- `load_all` returns `[]` (not an exception) when the log file doesn't exist.
- `append_and_commit` is append-only — it never rewrites or drops an existing record. It commits and pushes to `base_branch` directly, not the phase branch. It skips the push when there is nothing to commit.
- `append_and_commit` is safe under concurrent calls. Given two overlapping calls with distinct records, both records are present afterward (no lost update). It achieves this by re-fetching and retrying on a rejected `push_branch`, not by any external lock. A call whose record already made it into the log on a prior retry attempt does not append a duplicate.
- `append_and_commit` writes `log_json` atomically (temp file + `os.replace`): a process killed mid-write never leaves a truncated/invalid `log_json` on disk for a subsequent `load_all` to choke on.
- `uv run pytest` passes for both test files. `ruff check` and `ty` pass with no new violations.

## Manual test checklist
- Run `uv run pytest tests/build/test_manual_test.py tests/build/test_completion_log.py -v`. Confirm all cases above pass, including the `strict`-gated raise/no-raise behavior.
- Manually run `prompt` against a fixture checklist. Answer "fail," then "retry." Confirm the returned outcome has `retry=True`.
- Repeat, answering "fail" then "don't retry," with `strict=False`. Confirm there is no exception and `passed=False`.
- Repeat the same fail/don't-retry sequence with `strict=True` and confirm `ManualTestFailed` is raised.
- Against a scratch git repo, call `append_and_commit` twice with two different records. Confirm both are present afterward (append-only). Confirm the second call's diff/commit reflects the union of the log, not just the newest record.
- Simulate a concurrent conflict. Push two different records via `append_and_commit` from two clones of the same repo, without either clone re-fetching in between: call the first through to completion, then force the second's initial `fetch_resync` snapshot to predate it. Confirm the second call retries and lands both records, rather than losing one or raising.
- Confirm no unhandled exceptions appear in any of the above.

## Depends on
- Phase 09 merged.
