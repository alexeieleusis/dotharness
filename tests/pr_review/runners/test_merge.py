import json
import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from dotharness.code_host import (
    AuthenticationError,
    ChangeRequestState,
    CheckStatus,
    CodeHostError,
    CodeHostTimeoutError,
    MergeableState,
    RepositoryRef,
    UnsupportedCapabilityError,
)
from dotharness.code_host.testing import InMemoryCodeHost
from dotharness.pr_review.config import HarnessConfig, HarnessSection, MergeConfig, RepoConfig, _fallback_repo_slug
from dotharness.pr_review.runners import merge
from dotharness.pr_review.runners.merge import MergeError

P = "dotharness.pr_review.runners.merge."
SHA = "abc123"
REPO = RepositoryRef("acme/frontend")


def _cfg(tmp_path, enabled=True, max_lines=500, viewed=True):
    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        merge=MergeConfig(enabled=enabled, max_changed_lines=max_lines, require_all_files_viewed=viewed),
    )


def _write_record(cfg, number, sha=SHA, raw=None):
    path = merge.gate_record_path(cfg.repo_slug, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"head_sha": sha}), encoding="utf-8")


def _update(host, number, **fields):
    items = host._change_requests[REPO]
    for i, item in enumerate(items):
        if item.number == number:
            items[i] = replace(item, **fields)
    host._version += 1


def _seed(host, cfg, number=1, *, author="me", pr_state="ok", ci="pass", record=SHA, lines=(100, 20), viewed="all"):
    """One PR that passes every condition, then bent by the keyword arguments."""
    host.add_change_request(REPO, head_reference=f"feat-{number}", author=author, head_revision=SHA, number=number)
    fields = {"mergeable": MergeableState.MERGEABLE, "additions": lines[0], "deletions": lines[1]}
    fields.update(
        {
            "ok": {},
            "draft": {"is_draft": True},
            "conflict": {"mergeable": MergeableState.CONFLICTING},
            "unknown": {"mergeable": MergeableState.UNKNOWN},
            "auto": {"auto_merge_enabled": True},
        }[pr_state]
    )
    _update(host, number, **fields)
    if ci != "none":
        status = CheckStatus.FAIL if ci == "fail" else CheckStatus.PASS
        host.add_check(REPO, number, name="build", status=status, required=True)
    host._viewed[(REPO, number)] = {
        "all": {"a.py": True, "b.py": True},
        "some": {"a.py": True, "b.py": False},
        "empty": {},
    }[viewed]
    if record is not None:
        _write_record(cfg, number, sha=record)


@pytest.fixture
def host():
    return InMemoryCodeHost(repository="acme/frontend", user="me")


@pytest.fixture(autouse=True)
def _env(tmp_path, host):
    with (
        patch.object(merge.state, "XDG_DATA", tmp_path / "xdg"),
        patch(P + "acquire_lock"),
        patch("dotharness.pr_review.runners.common.create_code_host", side_effect=lambda _selection: host.code_host()),
    ):
        yield


def _ops(host):
    return [c.operation for c in host.calls]


def _merged(host):
    return [c for c in host.writes if c.operation == "ChangeRequests.squash_merge"]


async def _run(cfg, **kw):
    await merge.run(cfg, **kw)


async def test_disabled_makes_no_calls(tmp_path, host, caplog):
    cfg = _cfg(tmp_path, enabled=False)
    with patch(P + "acquire_lock") as lock, caplog.at_level(logging.INFO):
        await _run(cfg)
    assert host.calls == []
    lock.assert_not_called()
    assert "merge is disabled, nothing to do" in caplog.text


async def test_all_conditions_met_merges_once(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    await _run(cfg)
    assert [(c.args, c.kwargs) for c in _merged(host)] == [((REPO, 1), {"expected_head": SHA})]
    assert host.change_requests[REPO][0].state is ChangeRequestState.MERGED
    assert [c.operation for c in host.writes] == ["ChangeRequests.squash_merge"]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"ci": "fail"}, "ci-blocked"),
        ({"pr_state": "conflict"}, "conflicting"),
        ({"pr_state": "draft"}, "draft"),
        ({"pr_state": "unknown"}, "mergeable-unknown"),
        ({"pr_state": "auto"}, "auto-merge"),
        ({"record": None}, merge.SKIP_GATE_RECORD_MISSING),
        ({"record": "old-sha"}, merge.SKIP_GATE_RECORD_STALE),
        ({"lines": (400, 101)}, merge.SKIP_OVER_MAX_CHANGED_LINES),
        ({"viewed": "some"}, merge.SKIP_UNVIEWED_FILES),
        ({"viewed": "empty"}, merge.SKIP_UNVIEWED_FILES),
    ],
)
async def test_skip_reasons_make_no_merge_call(tmp_path, host, caplog, kwargs, reason):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, **kwargs)
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert f"PR #1: skipped: {reason}" in caplog.text


async def test_pr_of_another_author_is_not_merged(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, author="someone-else")
    with caplog.at_level(logging.INFO):
        await _run(cfg, pr=1)  # a direct run reaches the author check; the list is already filtered
    assert _merged(host) == []
    assert f"PR #1: skipped: {merge.SKIP_NOT_AUTHOR}" in caplog.text


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        ("Checks.list_checks", merge.SKIP_CHECKS_READ_FAILED),
        ("ChangeRequests.viewed_files", merge.SKIP_VIEWED_READ_FAILED),
    ],
)
async def test_read_failures_skip_without_merging(tmp_path, host, caplog, operation, reason):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next(operation, CodeHostError("boom"))
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert f"PR #1: skipped: {reason}" in caplog.text


def _fail_get_on(host, nth, error):
    """Make the ``nth`` (1-based) ``ChangeRequests.get`` call fail; the others reach the host."""
    real = host.code_host().change_requests
    calls = {"n": 0}
    original = real.get

    async def get(repo, number):
        calls["n"] += 1
        if calls["n"] == nth:
            raise error
        return await original(repo, number)

    real.get = get
    return real


async def test_state_read_failure_skips(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next("ChangeRequests.get", CodeHostError("boom"))
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert f"PR #1: skipped: {merge.SKIP_STATE_READ_FAILED}" in caplog.text


async def test_size_read_failure_skips(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    facet = _fail_get_on(host, 2, CodeHostError("boom"))  # the first get is the state read
    with (
        patch(
            "dotharness.pr_review.runners.common.create_code_host",
            side_effect=lambda _s: replace(host.code_host(), change_requests=facet),
        ),
        caplog.at_level(logging.INFO),
    ):
        await _run(cfg)
    assert _merged(host) == []
    assert f"PR #1: skipped: {merge.SKIP_SIZE_READ_FAILED}" in caplog.text


@pytest.mark.parametrize("fields", [{"additions": None}, {"deletions": None}, {"additions": None, "deletions": None}])
async def test_unreported_size_skips(tmp_path, host, caplog, fields):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    _update(host, 1, **fields)
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert f"PR #1: skipped: {merge.SKIP_SIZE_READ_FAILED}" in caplog.text


async def test_unreadable_record_blocks(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, record=None)
    _write_record(cfg, 1, raw="not json")
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert merge.SKIP_GATE_RECORD_MISSING in caplog.text


async def test_max_changed_lines_boundary_merges(tmp_path, host):
    cfg = _cfg(tmp_path, max_lines=120)
    _seed(host, cfg, lines=(100, 20))
    await _run(cfg)
    assert len(_merged(host)) == 1


async def test_unviewed_files_ignored_when_not_required(tmp_path, host):
    cfg = _cfg(tmp_path, viewed=False)
    _seed(host, cfg, viewed="some")
    await _run(cfg)
    assert len(_merged(host)) == 1
    assert "ChangeRequests.viewed_files" not in _ops(host)


async def test_dry_run_makes_no_merge_call(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    with caplog.at_level(logging.INFO):
        await _run(cfg, dry_run=True)
    assert host.writes == []
    assert "would merge" in caplog.text


async def test_pr_flag_processes_only_that_pr(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, 1)
    _seed(host, cfg, 2)
    await _run(cfg, pr=1)
    assert "ChangeRequests.list" not in _ops(host)
    assert [c.args[1] for c in _merged(host)] == [1]


async def test_only_own_open_prs_are_listed(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, 1)
    host.add_change_request(REPO, head_reference="theirs", author="someone-else", number=2)
    await _run(cfg)
    assert [c.args[1] for c in _merged(host)] == [1]
    listing = next(c for c in host.calls if c.operation == "ChangeRequests.list")
    assert listing.kwargs["author"] == "me"
    assert listing.kwargs["state"] is ChangeRequestState.OPEN


async def test_merge_failure_raises_merge_error_after_loop(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next("ChangeRequests.squash_merge", CodeHostError("head moved"))
    with pytest.raises(MergeError, match="#1") as exc:
        await _run(cfg)
    assert "head moved" in str(exc.value)


async def test_merge_failure_on_first_pr_does_not_abort_the_loop(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, 1)
    _seed(host, cfg, 2)
    host.fail_next("ChangeRequests.squash_merge", CodeHostError("boom"))
    with pytest.raises(MergeError, match="#1") as exc:
        await _run(cfg)
    assert "#2" not in str(exc.value)
    assert [c.args[1] for c in host.calls if c.operation == "ChangeRequests.squash_merge"] == [1, 2]


@pytest.mark.parametrize("applied", [True, False])
async def test_merge_timeout_is_a_failure_and_is_never_retried(tmp_path, host, applied):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next("ChangeRequests.squash_merge", CodeHostTimeoutError("timed out", write=True), apply=applied)
    with pytest.raises(MergeError, match="timed out"):
        await _run(cfg)
    attempts = [c for c in host.calls if c.operation == "ChangeRequests.squash_merge"]
    assert len(attempts) == 1  # no retry, no fallback
    merged = host.change_requests[REPO][0].state is ChangeRequestState.MERGED
    assert merged is applied  # the timeout may have taken effect


async def test_unsupported_merge_capability_stops_the_run(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, 1)
    _seed(host, cfg, 2)
    host.fail_next("ChangeRequests.squash_merge", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with pytest.raises(UnsupportedCapabilityError):
        await _run(cfg)
    assert [c.args[1] for c in host.calls if c.operation == "ChangeRequests.squash_merge"] == [1]


async def test_unsupported_state_read_stops_the_run(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next("ChangeRequests.get", UnsupportedCapabilityError("no", capability="ChangeRequests"))
    with pytest.raises(UnsupportedCapabilityError):
        await _run(cfg)
    assert host.writes == []


async def test_list_failure_is_a_collected_failure_not_a_raw_error(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next("ChangeRequests.list", CodeHostError("listing broke"))
    with pytest.raises(MergeError, match="listing broke"):
        await _run(cfg)
    assert host.writes == []


async def test_blank_current_user_is_not_author(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.set_user("")
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert merge.SKIP_NOT_AUTHOR in caplog.text


async def test_unresolvable_current_user_fails_open_to_no_merge(tmp_path, host, caplog):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    host.fail_next("Session.current_user", AuthenticationError("expired"))
    with caplog.at_level(logging.INFO):
        await _run(cfg)
    assert _merged(host) == []
    assert merge.SKIP_NOT_AUTHOR in caplog.text


async def test_never_approves(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg)
    await _run(cfg)
    assert not [op for op in _ops(host) if "approve" in op or op.startswith("Discussion.")]
    assert [c.operation for c in host.writes] == ["ChangeRequests.squash_merge"]


async def test_stacked_pr_uses_the_stack_root_required_checks(tmp_path, host):
    cfg = _cfg(tmp_path)
    _seed(host, cfg, 1, ci="pass")  # the root PR, head feat-1, required check "build"
    _seed(host, cfg, 4, ci="none")
    _update(host, 4, base_reference="feat-1")
    host.add_check(REPO, 4, name="build", status=CheckStatus.PASS)
    await _run(cfg, pr=4)
    finds = [c for c in host.calls if c.operation == "ChangeRequests.find_by_branch"]
    assert [c.args[1] for c in finds] == ["feat-1"]
    assert [c.args[1] for c in _merged(host)] == [4]


_GOLDEN = json.loads((Path(__file__).resolve().parents[2] / "contracts/gate_record_golden.json").read_text())


@pytest.mark.parametrize("case", _GOLDEN["path_cases"], ids=lambda c: c["slug"])
def test_golden_paths_and_slugs(case, tmp_path, monkeypatch):
    monkeypatch.setattr(merge.state, "XDG_DATA", tmp_path)
    cfg = _cfg(tmp_path)
    cfg.repo = RepoConfig(case["repo_name"], tmp_path)
    assert cfg.repo_slug == case["slug"]
    assert merge.gate_record_path(case["slug"], case["pr"]) == tmp_path / case["relative_path"]


@pytest.mark.parametrize("case", _GOLDEN["fallback_slug_cases"], ids=lambda c: c["slug"])
def test_golden_fallback_slug(case):
    assert _fallback_repo_slug(Path(case["working_dir"])) == case["slug"]


@pytest.mark.parametrize("slug", _GOLDEN["valid_slugs"])
def test_golden_valid_slugs(slug):
    assert merge.state._validate_repo_slug(slug) == slug


@pytest.mark.parametrize("slug", _GOLDEN["invalid_slugs"])
def test_golden_invalid_slugs(slug):
    with pytest.raises(ValueError, match="Invalid repo slug"):
        merge.state._validate_repo_slug(slug)


def test_golden_record_is_read_back(tmp_path, monkeypatch):
    monkeypatch.setattr(merge.state, "XDG_DATA", tmp_path)
    path = merge.gate_record_path("acme-frontend", 1)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_GOLDEN["record"]), encoding="utf-8")
    assert merge.read_gate_record("acme-frontend", 1) == _GOLDEN["record"]["head_sha"]
