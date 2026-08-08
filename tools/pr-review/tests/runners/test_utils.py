"""Shared fixtures and helpers for runner tests to eliminate DRY violations.

Each runner module has a base set of mocks required for _run_locked. Adding a
new dependency to any runner now requires changing only the corresponding
*_base_skip or *_base_full dict here, rather than every test method.

Usage:
    from tests.runners.test_utils import with_patches, rr_base_skip

    def test_foo():
        with with_patches("harness.runners.review_requested", **{**rr_base_skip, "_has_user_approved": True}) as m:
            ...
"""

from unittest.mock import MagicMock, patch


def with_patches(module, **overrides):
    """Context manager that applies patch.multiple() and yields the mocks dict."""
    _patcher = patch.multiple(module, **overrides)
    _patcher.start()
    try:
        yield _patcher.create_mock_dict()
    finally:
        _patcher.stop()


# ── review_requested ────────────────────────────────────────────────────────

_RR = "harness.runners.review_requested"

rr_base_skip = {
    "get_gh_token": MagicMock(return_value="tok"),
    "get_current_user": MagicMock(return_value="me"),
    "_get_prs": MagicMock(return_value=[{"number": 1, "url": "u", "headRefName": "b"}]),
    "_has_user_approved": MagicMock(return_value=False),
    "has_review_summary_comment": MagicMock(return_value=False),
    "git_detach_and_record": MagicMock(return_value="sha"),
    "Backend": MagicMock(),
}

rr_base_full = {
    **rr_base_skip,
    "git_fetch_and_checkout": MagicMock(),
    "git_restore": MagicMock(),
    "run_cmd": MagicMock(return_value=MagicMock(returncode=0, stdout=b"")),
    "get_pr_base_branch": MagicMock(return_value="main"),
    "get_pr_head_sha": MagicMock(return_value="abc123"),
    "get_changed_files": MagicMock(return_value=["src/foo.py"]),
    "get_file_diff": MagicMock(return_value="@@diff"),
    "os": MagicMock(),
}


# ── self_review ─────────────────────────────────────────────────────────────

_SR = "harness.runners.self_review"

sr_base_skip = {
    "get_gh_token": MagicMock(return_value="tok"),
    "get_current_user": MagicMock(return_value="alice"),
    "_list_my_prs": MagicMock(return_value=[{"number": 5, "url": "u", "headRefName": "b"}]),
    "git_detach_and_record": MagicMock(return_value="sha"),
    "Backend": MagicMock(),
}

sr_base_full = {
    **sr_base_skip,
    "has_review_summary_comment": MagicMock(return_value=False),
    "git_fetch_and_checkout": MagicMock(),
    "git_restore": MagicMock(),
    "get_pr_base_branch": MagicMock(return_value="main"),
    "get_pr_head_sha": MagicMock(return_value="abc123"),
    "get_changed_files": MagicMock(return_value=["src/foo.py"]),
    "get_file_diff": MagicMock(return_value="@@diff"),
    "os": MagicMock(),
}


# ── address_comments ────────────────────────────────────────────────────────

_AC = "harness.runners.address_comments"

ac_base_skip = {
    "get_gh_token": MagicMock(return_value="tok"),
    "_list_prs_to_check": MagicMock(return_value=[{"number": 1, "headRefName": "b"}]),
    "_has_pending_feedback": MagicMock(return_value=False),
    "git_detach_and_record": MagicMock(return_value="sha"),
    "Backend": MagicMock(),
}

ac_base_full = {
    **ac_base_skip,
    "fetch_pr_comments": MagicMock(return_value=[]),
    "git_fetch_and_checkout": MagicMock(),
    "git_restore": MagicMock(),
    "run_cmd": MagicMock(return_value=MagicMock(returncode=0, stdout=b"", stderr=b"")),
}
