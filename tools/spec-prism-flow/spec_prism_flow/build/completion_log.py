from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from spec_prism_flow.build import git_ops
from spec_prism_flow.build.git_ops import GitCommandError

# Fixed, repo-relative location of the shared completion log inside the target clone
# -- the single source of truth, so toolchain.py and track_runner.py both import it
# rather than each hardcoding their own copy of the path.
COMPLETION_LOG_JSON_RELPATH = Path("docs/completion-log.json")


@dataclass(frozen=True)
class CompletionRecord:
    phase_number: int
    phase_name: str
    pr_number: int | None
    pr_url: str | None
    pr_opened_at: datetime | None
    pr_merged_at: datetime | None
    manual_test_first_try_pass: bool | None
    escalation_reason: str | None
    address_comments_cycles: int = 0
    pr_diff_files: int = 0
    pr_diff_lines_added: int = 0
    pr_diff_lines_removed: int = 0
    human_escalations: int = 0

    @property
    def pr_open_to_merge_seconds(self) -> float | None:
        if self.pr_opened_at is None or self.pr_merged_at is None:
            return None
        return (self.pr_merged_at - self.pr_opened_at).total_seconds()


def _record_to_dict(record: CompletionRecord) -> dict:
    data = asdict(record)
    data["pr_opened_at"] = record.pr_opened_at.isoformat() if record.pr_opened_at else None
    data["pr_merged_at"] = record.pr_merged_at.isoformat() if record.pr_merged_at else None
    return data


def _record_from_dict(data: dict) -> CompletionRecord:
    kwargs = dict(data)
    kwargs["pr_opened_at"] = datetime.fromisoformat(data["pr_opened_at"]) if data["pr_opened_at"] else None
    kwargs["pr_merged_at"] = datetime.fromisoformat(data["pr_merged_at"]) if data["pr_merged_at"] else None
    return CompletionRecord(**kwargs)


def load_all(log_json: Path) -> list[CompletionRecord]:
    """Returns `[]`, not an exception, when `log_json` doesn't exist -- the normal
    case before any phase has completed."""
    if not log_json.exists():
        return []
    payload = json.loads(log_json.read_text())
    return [_record_from_dict(item) for item in payload]


def _write_log_json(log_json: Path, records: list[CompletionRecord]) -> None:
    """Serializes `records` to `log_json` atomically: written to a temp file in the
    same directory first, then `os.replace`d onto `log_json`, so a crash or kill
    mid-write can never leave `log_json` truncated or invalid for a later `load_all`
    to choke on -- the previous, fully-written version stays in place until the
    replace succeeds."""
    log_json.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps([_record_to_dict(r) for r in records], indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=log_json.parent, prefix=f".{log_json.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp_name, log_json)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "None"
    total = int(seconds)
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = [f"{days}d"] if days else []
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _render_log_md(records: list[CompletionRecord]) -> str:
    header = (
        "| Phase | PR | Address cycles | Open→merge | Diff | Escalations | Manual test |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
    )
    rows = []
    for record in sorted(records, key=lambda r: r.phase_number):
        phase = f"{record.phase_number:02d} -- {record.phase_name}"
        pr = f"[#{record.pr_number}]({record.pr_url})" if record.pr_number is not None and record.pr_url else "None"
        open_to_merge = _format_duration(record.pr_open_to_merge_seconds)
        diff = f"+{record.pr_diff_lines_added}/-{record.pr_diff_lines_removed} ({record.pr_diff_files} files)"
        escalations = (
            f"{record.human_escalations} -- {record.escalation_reason}"
            if record.escalation_reason
            else str(record.human_escalations)
        )
        if record.manual_test_first_try_pass is None:
            manual_test = "None"
        elif record.manual_test_first_try_pass:
            manual_test = "Passed first try"
        else:
            manual_test = "Failed first try"
        rows.append(
            f"| {phase} | {pr} | {record.address_comments_cycles} | {open_to_merge} | {diff} | {escalations} | {manual_test} |"
        )
    return header + "\n".join(rows) + ("\n" if rows else "")


def append_and_commit(
    clone: Path,
    log_json: Path,
    log_md: Path,
    record: CompletionRecord,
    *,
    base_branch: str,
    max_conflict_retries: int = 5,
) -> None:
    """Appends `record` to the shared completion log on `base_branch` and pushes,
    safe to call concurrently from Phase 12's parallel mode where multiple leaves
    finish around the same time with no external lock: each attempt re-fetches
    `base_branch` fresh via `git_ops.fetch_resync`, then pushes with
    `git_ops.push_branch`'s `expect_sha` pinned to that fetch's sha, so the remote
    itself atomically rejects the push if another concurrent call's push landed in
    between -- a separate preflight check here couldn't close that gap, since the
    remote could still move between the preflight and the push. A rejected push, or a
    transient `GitCommandError` from the `fetch_resync`/`head_sha` pair itself, is
    retried the same way, looping back to `fetch_resync` against the new tip, up to
    `max_conflict_retries` times before the last failure propagates. A record
    already present under `record.phase_number` (a prior attempt's push that landed
    even though this call later saw it as a conflict) is never duplicated --
    `commit_all` then finds nothing new to commit, so the push is skipped entirely
    and this returns cleanly."""
    if max_conflict_retries < 1:
        raise ValueError("max_conflict_retries must be >= 1")  # noqa: TRY003
    last_exc: GitCommandError | None = None
    for _ in range(max_conflict_retries):
        try:
            git_ops.fetch_resync(clone, base_branch)
            base_sha = git_ops.head_sha(clone)
        except GitCommandError as exc:
            last_exc = exc
            continue
        records = load_all(log_json)
        if not any(existing.phase_number == record.phase_number for existing in records):
            records = [*records, record]
        _write_log_json(log_json, records)
        log_md.write_text(_render_log_md(records))
        if not git_ops.commit_all(clone, f"Record phase {record.phase_number} completion ({record.phase_name})"):
            return
        try:
            git_ops.push_branch(clone, base_branch, expect_sha=base_sha)
        except GitCommandError as exc:
            last_exc = exc
            continue
        else:
            return
    if last_exc is not None:
        raise last_exc
