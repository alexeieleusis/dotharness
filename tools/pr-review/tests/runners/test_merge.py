import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from harness.config import HarnessConfig, HarnessSection, MergeConfig, RepoConfig, _fallback_repo_slug
from harness.runners import merge
from harness.runners.auto_approve_github import SafetyResult, SafetyStatus
from harness.runners.merge import MergeError

P = "harness.runners.merge."
SHA = "abc123"


def _cfg(tmp_path, enabled=True, max_lines=500, viewed=True):
    return HarnessConfig(
        harness=HarnessSection(
            "opencode", "echo tok", knowledge_dir=tmp_path / "k", path_prepend=[], env={}, backend_timeout_seconds=10
        ),
        repo=RepoConfig("acme/frontend", tmp_path),
        merge=MergeConfig(enabled=enabled, max_changed_lines=max_lines, require_all_files_viewed=viewed),
    )


def _proc(returncode=0, stdout=b"", stderr=b""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _write_record(tmp_path, cfg, number, sha=SHA, raw=None):
    path = merge.gate_record_path(cfg.repo_slug, number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps({"head_sha": sha}), encoding="utf-8")


@contextlib.contextmanager
def _mocks(
    tmp_path,
    cfg,
    prs=None,
    author="me",
    pr_state="ok",
    ci="pass",
    record=SHA,
    lines=(100, 20),
    viewed="all",
    merge_rc=0,
    size_proc=None,
):
    prs = [{"number": 1, "author": {"login": author}}] if prs is None else prs
    calls: list[list[str]] = []

    def fake_gh(args, env):
        calls.append(args)
        if args[:2] == ["pr", "list"]:
            return _proc(stdout=json.dumps(prs).encode())
        if args[:2] == ["pr", "view"] and "number,author" in args:
            return _proc(stdout=json.dumps(prs[0]).encode())
        if args[:2] == ["pr", "view"] and "additions,deletions" in args:
            if size_proc is not None:
                return size_proc
            return _proc(stdout=json.dumps({"additions": lines[0], "deletions": lines[1]}).encode())
        if args[:2] == ["pr", "merge"]:
            rc = merge_rc.get(int(args[2]), 0) if isinstance(merge_rc, dict) else merge_rc
            return _proc(returncode=rc, stderr=b"boom")
        pytest.fail(f"unexpected gh call {args}")

    states = {
        "ok": {
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "autoMergeRequest": None,
            "headRefOid": SHA,
        },
        "draft": {"state": "OPEN", "isDraft": True, "mergeable": "MERGEABLE", "headRefOid": SHA},
        "conflict": {"state": "OPEN", "isDraft": False, "mergeable": "CONFLICTING", "headRefOid": SHA},
        "unknown": {"state": "OPEN", "isDraft": False, "mergeable": "UNKNOWN", "headRefOid": SHA},
        "auto": {
            "state": "OPEN",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "autoMergeRequest": {"mergeMethod": "SQUASH"},
            "headRefOid": SHA,
        },
        "unreadable": None,
    }
    ci_results = {
        "pass": None,
        "fail": SafetyResult(SafetyStatus.SKIP, "ci-blocked"),
        "unreadable": SafetyResult(SafetyStatus.READ_FAILED, "checks read failed"),
    }
    viewed_results = {
        "all": ("node", {"a.py": "VIEWED", "b.py": "VIEWED"}),
        "some": ("node", {"a.py": "VIEWED", "b.py": "UNVIEWED"}),
        "empty": ("node", {}),
        "unreadable": None,
    }
    with (
        patch.object(merge.state, "XDG_DATA", tmp_path / "xdg"),
        patch(P + "acquire_lock"),
        patch(P + "get_gh_token", return_value="tok"),
        patch(P + "build_subprocess_env", return_value={}),
        patch(P + "get_current_user", return_value="me"),
        patch(P + "_gh", side_effect=fake_gh),
        patch(P + "_read_pr_state", return_value=states[pr_state]),
        patch(P + "_check_ci", return_value=ci_results[ci]),
        patch(P + "read_viewed_states", return_value=viewed_results[viewed]),
    ):
        if record is not None:
            for pr in prs:
                _write_record(tmp_path, cfg, pr["number"], sha=record)
        yield calls


def _merge_calls(calls):
    return [c for c in calls if c[:2] == ["pr", "merge"]]


def _run(cfg, **kw):
    merge.run(cfg, **kw)


def test_disabled_makes_no_calls(tmp_path, caplog):
    cfg = _cfg(tmp_path, enabled=False)
    with patch(P + "_gh") as gh, patch(P + "acquire_lock") as lock, caplog.at_level(logging.INFO):
        _run(cfg)
    gh.assert_not_called()
    lock.assert_not_called()
    assert "merge is disabled, nothing to do" in caplog.text


def test_all_conditions_met_merges_once(tmp_path):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg) as calls:
        _run(cfg)
    merges = _merge_calls(calls)
    assert merges == [["pr", "merge", "1", "--repo", "acme/frontend", "--squash", "--match-head-commit", SHA]]


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"ci": "fail"}, "ci-blocked"),
        ({"ci": "unreadable"}, merge.SKIP_CHECKS_READ_FAILED),
        ({"pr_state": "conflict"}, "conflicting"),
        ({"pr_state": "draft"}, "draft"),
        ({"pr_state": "unknown"}, "mergeable-unknown"),
        ({"pr_state": "auto"}, "auto-merge"),
        ({"pr_state": "unreadable"}, merge.SKIP_STATE_READ_FAILED),
        ({"record": None}, merge.SKIP_GATE_RECORD_MISSING),
        ({"record": "old-sha"}, merge.SKIP_GATE_RECORD_STALE),
        ({"lines": (400, 101)}, merge.SKIP_OVER_MAX_CHANGED_LINES),
        ({"size_proc": _proc(returncode=1)}, merge.SKIP_SIZE_READ_FAILED),
        ({"size_proc": _proc(stdout=b"not json")}, merge.SKIP_SIZE_READ_FAILED),
        ({"size_proc": _proc(stdout=b'{"additions": null, "deletions": 3}')}, merge.SKIP_SIZE_READ_FAILED),
        ({"size_proc": _proc(stdout=b"{}")}, merge.SKIP_SIZE_READ_FAILED),
        ({"viewed": "some"}, merge.SKIP_UNVIEWED_FILES),
        ({"viewed": "empty"}, merge.SKIP_UNVIEWED_FILES),
        ({"viewed": "unreadable"}, merge.SKIP_VIEWED_READ_FAILED),
        ({"author": "someone-else"}, merge.SKIP_NOT_AUTHOR),
    ],
)
def test_skip_reasons_make_no_merge_call(tmp_path, caplog, kwargs, reason):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg, **kwargs) as calls, caplog.at_level(logging.INFO):
        _run(cfg)
    assert _merge_calls(calls) == []
    assert f"PR #1: skipped: {reason}" in caplog.text


def test_unreadable_record_blocks(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg, record=None) as calls, caplog.at_level(logging.INFO):
        _write_record(tmp_path, cfg, 1, raw="not json")
        _run(cfg)
    assert _merge_calls(calls) == []
    assert merge.SKIP_GATE_RECORD_MISSING in caplog.text


def test_max_changed_lines_boundary_merges(tmp_path):
    cfg = _cfg(tmp_path, max_lines=120)
    with _mocks(tmp_path, cfg, lines=(100, 20)) as calls:
        _run(cfg)
    assert len(_merge_calls(calls)) == 1


def test_unviewed_files_ignored_when_not_required(tmp_path):
    cfg = _cfg(tmp_path, viewed=False)
    with _mocks(tmp_path, cfg, viewed="some") as calls:
        _run(cfg)
    assert len(_merge_calls(calls)) == 1


def test_dry_run_makes_no_merge_call(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg) as calls, caplog.at_level(logging.INFO):
        _run(cfg, dry_run=True)
    assert _merge_calls(calls) == []
    assert "would merge" in caplog.text


def test_pr_flag_processes_only_that_pr(tmp_path):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg) as calls:
        _run(cfg, pr=1)
    assert not any(c[:2] == ["pr", "list"] for c in calls)
    assert len(_merge_calls(calls)) == 1


def test_merge_failure_raises_merge_error_after_loop(tmp_path):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg, merge_rc=1), pytest.raises(MergeError, match="#1"):
        _run(cfg)


def test_merge_failure_on_first_pr_does_not_abort_the_loop(tmp_path):
    cfg = _cfg(tmp_path)
    prs = [{"number": n, "author": {"login": "me"}} for n in (1, 2)]
    with _mocks(tmp_path, cfg, prs=prs, merge_rc={1: 1}) as calls:
        with pytest.raises(MergeError, match="#1") as exc:
            _run(cfg)
        assert "#2" not in str(exc.value)
        assert [c[2] for c in _merge_calls(calls)] == ["1", "2"]


def test_blank_current_user_is_not_author(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    with (
        _mocks(tmp_path, cfg, author="") as calls,
        patch(P + "get_current_user", return_value=""),
        caplog.at_level(logging.INFO),
    ):
        _run(cfg)
    assert _merge_calls(calls) == []
    assert merge.SKIP_NOT_AUTHOR in caplog.text


def test_never_approves(tmp_path):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg) as calls:
        _run(cfg)
    flat = [a for c in calls for a in c]
    assert "review" not in flat
    assert "--approve" not in flat
    for flag in ("--admin", "--auto", "--delete-branch"):
        assert flag not in flat


def test_malformed_pr_list_output_is_a_collected_failure_not_a_raw_error(tmp_path):
    cfg = _cfg(tmp_path)
    with _mocks(tmp_path, cfg) as calls:
        with (
            patch(P + "_gh", return_value=_proc(stdout=b"not json")),
            pytest.raises(MergeError, match="gh pr output was not JSON"),
        ):
            _run(cfg)
        assert _merge_calls(calls) == []


_GOLDEN = json.loads((Path(__file__).resolve().parents[3] / "contracts/gate_record_golden.json").read_text())


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
