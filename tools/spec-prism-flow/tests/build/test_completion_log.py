import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock, call

import pytest

from spec_prism_flow.build import completion_log
from spec_prism_flow.build.completion_log import CompletionRecord
from spec_prism_flow.build.git_ops import GitCommandError

_BASE_RECORD = CompletionRecord(
    phase_number=7,
    phase_name="build-agent-runner-leaf",
    pr_number=53,
    pr_url="https://github.com/acme/repo/pull/53",
    pr_opened_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    pr_merged_at=datetime(2026, 1, 1, 14, 30, 0, tzinfo=UTC),
    manual_test_first_try_pass=True,
    escalation_reason=None,
)


def _record(phase_number: int = 7, **overrides) -> CompletionRecord:
    overrides.setdefault("pr_url", f"https://github.com/acme/repo/pull/{phase_number}")
    return replace(_BASE_RECORD, phase_number=phase_number, **overrides)


# --- CompletionRecord -------------------------------------------------------


def test_pr_open_to_merge_seconds_computed_from_open_and_merge_timestamps():
    record = _record()
    assert record.pr_open_to_merge_seconds == 2.5 * 3600


def test_pr_open_to_merge_seconds_is_none_when_either_timestamp_missing():
    assert _record(pr_opened_at=None).pr_open_to_merge_seconds is None
    assert _record(pr_merged_at=None).pr_open_to_merge_seconds is None


def test_completion_record_has_no_track_or_sonar_fields():
    record_fields = CompletionRecord.__dataclass_fields__.keys()
    assert "track" not in record_fields
    assert not any(name.startswith("sonar_issues_") for name in record_fields)
    assert "lensflow_attributable_issues" not in record_fields


# --- load_all ----------------------------------------------------------------


def test_load_all_returns_empty_list_when_file_missing(tmp_path):
    assert completion_log.load_all(tmp_path / "log.json") == []


def test_load_all_round_trips_datetime_and_none_fields(tmp_path):
    log_json = tmp_path / "log.json"
    record = _record(pr_number=None, pr_url=None, pr_opened_at=None, pr_merged_at=None, escalation_reason="timed out")
    completion_log._write_log_json(log_json, [record])

    loaded = completion_log.load_all(log_json)

    assert loaded == [record]


# --- _write_log_json atomicity -------------------------------------------------


def test_write_log_json_leaves_original_file_intact_if_replace_fails(tmp_path, monkeypatch):
    log_json = tmp_path / "log.json"
    log_json.write_text("[]")
    monkeypatch.setattr(completion_log.os, "replace", Mock(side_effect=OSError("boom")))

    with pytest.raises(OSError):
        completion_log._write_log_json(log_json, [_record()])

    assert log_json.read_text() == "[]"
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_log_json_writes_valid_indented_json(tmp_path):
    log_json = tmp_path / "log.json"
    completion_log._write_log_json(log_json, [_record()])

    payload = json.loads(log_json.read_text())
    assert payload[0]["phase_number"] == 7
    assert "\n" in log_json.read_text()  # indent=2 produces multi-line output


# --- append_and_commit ---------------------------------------------------------


def _patch_git_ops(
    monkeypatch,
    *,
    remote_sha: str = "deadbeef",
    commit_all_result: bool = True,
    push_side_effect=None,
):
    """Patches `git_ops.fetch_resync`/`head_sha`/`commit_all`/`push_branch` so
    `append_and_commit`'s retry logic can be exercised without a real git repo.
    `push_branch` is called with `expect_sha` pinned to whatever `fetch_resync`'s
    `head_sha` reported, so the remote itself -- not a separate preflight check --
    is what rejects a push built on stale content; tests simulate that rejection by
    passing `push_side_effect`."""
    fetch_mock = Mock()
    head_sha_mock = Mock(return_value=remote_sha)
    commit_mock = Mock(return_value=commit_all_result)
    push_mock = Mock(side_effect=push_side_effect) if push_side_effect is not None else Mock()
    monkeypatch.setattr(completion_log.git_ops, "fetch_resync", fetch_mock)
    monkeypatch.setattr(completion_log.git_ops, "head_sha", head_sha_mock)
    monkeypatch.setattr(completion_log.git_ops, "commit_all", commit_mock)
    monkeypatch.setattr(completion_log.git_ops, "push_branch", push_mock)
    return fetch_mock, commit_mock, push_mock


def test_append_and_commit_writes_and_pushes_on_first_try(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    fetch_mock, _, push_mock = _patch_git_ops(monkeypatch)

    completion_log.append_and_commit(clone, log_json, log_md, _record(), base_branch="main")

    fetch_mock.assert_called_once_with(clone, "main")
    push_mock.assert_called_once_with(clone, "main", expect_sha="deadbeef")
    assert json.loads(log_json.read_text())[0]["phase_number"] == 7
    assert "build-agent-runner-leaf" in log_md.read_text()


def test_append_and_commit_skips_push_when_nothing_to_commit(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    _, _, push_mock = _patch_git_ops(monkeypatch, commit_all_result=False)

    completion_log.append_and_commit(clone, log_json, log_md, _record(), base_branch="main")

    push_mock.assert_not_called()


def test_append_and_commit_does_not_duplicate_an_already_present_record(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    existing = _record(phase_number=7)
    completion_log._write_log_json(log_json, [existing])
    _, _, push_mock = _patch_git_ops(monkeypatch, commit_all_result=False)

    completion_log.append_and_commit(clone, log_json, log_md, _record(phase_number=7), base_branch="main")

    assert len(json.loads(log_json.read_text())) == 1
    push_mock.assert_not_called()


def test_append_and_commit_is_append_only_across_two_distinct_records(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    _patch_git_ops(monkeypatch)

    completion_log.append_and_commit(clone, log_json, log_md, _record(phase_number=7), base_branch="main")
    completion_log.append_and_commit(clone, log_json, log_md, _record(phase_number=8), base_branch="main")

    phase_numbers = {item["phase_number"] for item in json.loads(log_json.read_text())}
    assert phase_numbers == {7, 8}
    md_text = log_md.read_text()
    assert "07 -- build-agent-runner-leaf" in md_text
    assert "08 -- build-agent-runner-leaf" in md_text


def test_append_and_commit_retries_after_a_stale_lease_rejection(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    stale_lease = GitCommandError(["git", "push"], 1, "stale info")
    fetch_mock, _, push_mock = _patch_git_ops(monkeypatch, push_side_effect=[stale_lease, None])

    completion_log.append_and_commit(clone, log_json, log_md, _record(), base_branch="main", max_conflict_retries=5)

    assert fetch_mock.call_count == 2
    assert push_mock.call_count == 2


def test_append_and_commit_raises_last_error_after_exhausting_push_rejections(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    errors = [GitCommandError(["git", "push"], 1, f"stale info {i}") for i in range(3)]
    _, _, push_mock = _patch_git_ops(monkeypatch, push_side_effect=errors)

    with pytest.raises(GitCommandError) as exc_info:
        completion_log.append_and_commit(clone, log_json, log_md, _record(), base_branch="main", max_conflict_retries=3)

    assert exc_info.value is errors[-1]
    assert push_mock.call_count == 3


def test_append_and_commit_pushes_directly_to_base_branch_not_a_phase_branch(tmp_path, monkeypatch):
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    _, _, push_mock = _patch_git_ops(monkeypatch)

    completion_log.append_and_commit(clone, log_json, log_md, _record(), base_branch="main")

    assert push_mock.call_args == call(clone, "main", expect_sha="deadbeef")


def test_append_and_commit_pins_expect_sha_to_this_attempts_own_fetch_resync(tmp_path, monkeypatch):
    """Each retry re-fetches `base_branch` and must pin the *new* `head_sha` to its
    own `push_branch` call, not a sha from an earlier attempt -- otherwise a retry
    built on fresh content could be rejected against a stale expectation."""
    clone = tmp_path
    log_json = clone / "log.json"
    log_md = clone / "log.md"
    stale_lease = GitCommandError(["git", "push"], 1, "stale info")
    fetch_mock = Mock()
    head_sha_mock = Mock(side_effect=["first-sha", "second-sha"])
    commit_mock = Mock(return_value=True)
    push_mock = Mock(side_effect=[stale_lease, None])
    monkeypatch.setattr(completion_log.git_ops, "fetch_resync", fetch_mock)
    monkeypatch.setattr(completion_log.git_ops, "head_sha", head_sha_mock)
    monkeypatch.setattr(completion_log.git_ops, "commit_all", commit_mock)
    monkeypatch.setattr(completion_log.git_ops, "push_branch", push_mock)

    completion_log.append_and_commit(clone, log_json, log_md, _record(), base_branch="main", max_conflict_retries=5)

    assert push_mock.call_args_list == [
        call(clone, "main", expect_sha="first-sha"),
        call(clone, "main", expect_sha="second-sha"),
    ]


# --- log_md rendering ----------------------------------------------------------


def test_render_log_md_handles_none_fields_and_escalations():
    record = _record(
        pr_number=None,
        pr_url=None,
        pr_opened_at=None,
        pr_merged_at=None,
        manual_test_first_try_pass=None,
        escalation_reason="agent got stuck",
        human_escalations=2,
    )

    rendered = completion_log._render_log_md([record])

    assert "None" in rendered
    assert "2 -- agent got stuck" in rendered
    assert "Open→merge" in rendered


def test_render_log_md_sorts_rows_by_phase_number():
    rendered = completion_log._render_log_md([_record(phase_number=9), _record(phase_number=7)])

    assert rendered.index("07 -- ") < rendered.index("09 -- ")
