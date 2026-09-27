import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.config import HarnessConfig, HarnessSection, RegretReviewConfig, RepoConfig
from harness.runners.common import REGRET_REVIEW_MARKER, count_diff_lines
from harness.runners.regret_review import (
    IntroducingHunk,
    RegretFinding,
    _blamed_shas,
    _removed_modified_ranges,
    build_regret_comment_body,
    find_introducing_prs,
)


def _finding(**overrides) -> RegretFinding:
    base = {
        "path": "src/foo.py",
        "line": 42,
        "introducing_pr_number": 123,
        "comment_id": 456,
        "comment_body": "This edge case isn't handled — the counter wraps at MAX.",
        "comment_author": "alice",
        "comment_url": "https://github.com/acme/repo/pull/123#discussion_r456",
    }
    base.update(overrides)
    return RegretFinding(**base)


def test_build_regret_comment_body_single_finding():
    body = build_regret_comment_body([_finding()])
    assert body == (
        "# Regret Review\n"
        "\n"
        "### src/foo.py:42\n"
        "Introduced in #123 — https://github.com/acme/repo/pull/123#discussion_r456\n"
        "> This edge case isn't handled — the counter wraps at MAX.\n"
        "\n"
        f"{REGRET_REVIEW_MARKER}"
    )


def test_build_regret_comment_body_ends_with_marker_exactly_once():
    body = build_regret_comment_body([_finding(), _finding(path="src/bar.py", line=7)])
    assert body.endswith(REGRET_REVIEW_MARKER)
    assert body.count(REGRET_REVIEW_MARKER) == 1


def test_build_regret_comment_body_one_section_per_finding_in_order():
    body = build_regret_comment_body([
        _finding(path="src/a.py", line=1),
        _finding(path="src/b.py", line=2),
    ])
    assert body.count("\n### ") == 2
    assert body.count("# Regret Review") == 1
    assert body.index("### src/a.py:1") < body.index("### src/b.py:2")


def test_build_regret_comment_body_shows_pr_number_and_comment_url():
    body = build_regret_comment_body([_finding()])
    assert "#123" in body
    assert "https://github.com/acme/repo/pull/123#discussion_r456" in body


def test_build_regret_comment_body_short_body_not_truncated():
    body = build_regret_comment_body([_finding(comment_body="y" * 300)])
    assert "…" not in body
    assert "> " + "y" * 300 in body


def test_build_regret_comment_body_truncates_long_body_at_cap_with_inline_note():
    body = build_regret_comment_body([_finding(comment_body="x" * 350)])
    assert "> " + "x" * 300 + "…" in body
    assert "x" * 301 not in body


def test_build_regret_comment_body_multiline_excerpt_stays_one_blockquote():
    body = build_regret_comment_body([_finding(comment_body="line one\nline two")])
    assert "> line one\n> line two" in body
    assert body.count("> ") == 2


# --- phase 03: blame → introducing commit → introducing PR resolver ---


def _config(max_diff_lines=50, authors="*", timeout=300) -> HarnessConfig:
    return HarnessConfig(
        harness=HarnessSection(),
        repo=RepoConfig(name="acme/repo", working_dir=Path("wdir")),
        regret_review=RegretReviewConfig(
            enabled=True,
            max_diff_lines=max_diff_lines,
            authors=authors,
            regret_review_timeout=timeout,
        ),
    )


def _pr(number=42, login="alice") -> dict:
    return {
        "number": number,
        "url": "https://github.com/acme/repo/pull/42",
        "headRefName": "fix/typo",
        "author": {"login": login},
    }


def _run(returncode=0, stdout=b"", stderr=b"") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _cmd_calls(mock_run, program: str) -> list[list[str]]:
    return [c.args[0] for c in mock_run.call_args_list if c.args and c.args[0][0] == program]


def test_count_diff_lines_counts_added_and_removed_excluding_headers():
    diff = "--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n"
    assert count_diff_lines(diff) == 2
    assert count_diff_lines("") == 0


def test_removed_modified_ranges_parses_hunk_headers():
    diff = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -5,2 +5,2 @@\n"  # modified
        "@@ -8,0 +9,3 @@\n"  # pure addition — no range
        "@@ -12,1 +14,0 @@\n"  # pure deletion — range with empty new side
        "this line is not a hunk header\n"
    )
    assert _removed_modified_ranges(diff) == [(5, 2, 5, 2), (12, 1, 14, 0)]


def test_removed_modified_ranges_defaults_omitted_counts_to_one():
    diff = "@@ -5 +5 @@\n@@ -5 +5,2 @@\n@@ -5,1 +5 @@\n"
    assert _removed_modified_ranges(diff) == [(5, 1, 5, 1), (5, 1, 5, 2), (5, 1, 5, 1)]


def test_removed_modified_ranges_pure_addition_only_diff_has_no_ranges():
    diff = "--- a/x\n+++ b/x\n@@ -4,0 +5,2 @@\n+new one\n+new two\n"
    assert _removed_modified_ranges(diff) == []


def test_blamed_shas_parses_default_blame_lines_and_strips_boundary_prefix():
    output = (
        "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) line one\n"
        "^abc123def456 (alice 2026-01-01 09:00:00 +0000   6) line two\n"
        "a line that does not match the blame shape\n"
    )
    assert _blamed_shas(output) == ["abc123def456", "abc123def456"]


def test_find_introducing_prs_happy_path_blames_merge_base_and_resolves_pr(tmp_path):
    diff = (
        "diff --git a/src/foo.py b/src/foo.py\n"
        "index 1111..2222 100644\n"
        "--- a/src/foo.py\n"
        "+++ b/src/foo.py\n"
        "@@ -5,2 +5,2 @@\n"
        "-old one\n"
        "-old two\n"
        "+new one\n"
        "+new two\n"
    )
    blame = (
        "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old one\n"
        "abc123def456 (alice 2026-01-01 10:00:00 +0000   6) old two\n"
    )

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 7}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(), _config(), str(tmp_path), {})
    assert hunks == [
        IntroducingHunk(
            path="src/foo.py",
            old_start=5,
            old_end=6,
            new_start=5,
            new_end=6,
            introducing_sha="abc123def456",
            introducing_pr_number=7,
        )
    ]
    # Blame runs against the pre-fix state (the merge-base), never the fix's own commit.
    assert _cmd_calls(mock_run, "git") == [
        ["git", "diff", "origin/main...HEAD", "--unified=0", "--", "src/foo.py"],
        ["git", "merge-base", "origin/main", "HEAD"],
        ["git", "blame", "-L", "5,6", "mb123mb123mb123mb123mb123", "--", "src/foo.py"],
    ]
    assert _cmd_calls(mock_run, "gh") == [["gh", "api", "repos/acme/repo/commits/abc123def456/pulls"]]


def test_find_introducing_prs_skips_oversized_pr_with_no_blame_or_api(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,60 +1,0 @@\n" + "".join(f"-line {i}\n" for i in range(60))

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"gate must short-circuit before any blame/API call: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    assert mock_run.call_count == 1


def test_find_introducing_prs_exits_oversized_gate_mid_file_list(tmp_path):
    small = "--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    big = "--- a/b.py\n+++ b/b.py\n@@ -1,50 +1,0 @@\n" + "".join(f"-line {i}\n" for i in range(50))

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            if cmd[-1] == "a.py":
                return _run(0, small.encode())
            if cmd[-1] == "b.py":
                return _run(0, big.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["a.py", "b.py", "c.py"]),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    # 1 + 50 > 50 on the second file: the gate exits then, and c.py is never diffed.
    assert mock_run.call_count == 2


def test_find_introducing_prs_at_exact_max_diff_lines_is_not_gated(tmp_path):
    # 25 removed + 25 added = exactly max_diff_lines: at-or-below is bugfix-shaped.
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,25 +1,25 @@\n" + "".join(
        f"-old {i}\n+new {i}\n" for i in range(25)
    )

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(1)  # blame failure — the point is the gate did NOT trip first
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []


def test_find_introducing_prs_author_gate_skips_before_any_git_or_gh(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch") as mock_base,
        patch("harness.runners.regret_review.get_changed_files") as mock_files,
    ):
        assert find_introducing_prs(_pr(login="bob"), _config(authors="alice"), str(tmp_path), {}) == []
    mock_run.assert_not_called()
    mock_base.assert_not_called()
    mock_files.assert_not_called()


def test_find_introducing_prs_author_allowlist_match_proceeds(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 7}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(login="alice"), _config(authors=["alice", "bob"]), str(tmp_path), {})
    assert [h.introducing_pr_number for h in hunks] == [7]


def test_find_introducing_prs_missing_author_fails_closed_for_explicit_allowlist(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch") as mock_base,
        patch("harness.runners.regret_review.get_changed_files") as mock_files,
    ):
        pr = {"number": 42}
        assert find_introducing_prs(pr, _config(authors="alice"), str(tmp_path), {}) == []
    mock_run.assert_not_called()
    mock_base.assert_not_called()
    mock_files.assert_not_called()


def test_find_introducing_prs_pure_addition_diff_blames_nothing(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -4,0 +5,3 @@\n+new a\n+new b\n+new c\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        msg = f"pure additions have nothing to blame: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    # diff + merge-base only: no blame, no gh.
    assert [c.args[0][1] for c in mock_run.call_args_list] == ["diff", "merge-base"]


def test_find_introducing_prs_same_commit_blamed_from_two_ranges_is_resolved_once(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old a\n+new a\n@@ -9,1 +9,1 @@\n-old b\n+new b\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            line = "old a" if cmd[3] == "5,5" else "old b"
            return _run(0, f"abc123def456 (alice 2026-01-01 10:00:00 +0000) {line}\n".encode())
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 7}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(), _config(), str(tmp_path), {})
    assert [(h.old_start, h.old_end) for h in hunks] == [(5, 5), (9, 9)]
    assert all(h.introducing_sha == "abc123def456" and h.introducing_pr_number == 7 for h in hunks)
    # G4: one unique SHA → exactly one commit→PR lookup, not one per range.
    assert len(_cmd_calls(mock_run, "gh")) == 1


def test_find_introducing_prs_range_spanning_two_commits_yields_one_hunk_per_commit(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,2 +5,2 @@\n-old a\n-old b\n+new a\n+new b\n"
    blame = "aaa111aaa111 (alice 2026-01-01 10:00:00 +0000   5) old a\nbbb222bbb222 (bob 2026-01-02 11:00:00 +0000   6) old b\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            number = 7 if "aaa111aaa111" in cmd[2] else 8
            return _run(0, json.dumps([{"number": number}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(), _config(), str(tmp_path), {})
    assert sorted((h.introducing_sha, h.introducing_pr_number) for h in hunks) == [
        ("aaa111aaa111", 7),
        ("bbb222bbb222", 8),
    ]


def test_find_introducing_prs_commit_in_multiple_prs_yields_one_hunk_per_pr(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 8}, {"number": 7}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(), _config(), str(tmp_path), {})
    assert sorted(h.introducing_pr_number for h in hunks) == [7, 8]


def test_find_introducing_prs_zero_pr_result_skips_lines_silently(tmp_path, caplog):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, b"[]")
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
        caplog.at_level(logging.DEBUG, logger="harness.runners.regret_review"),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_find_introducing_prs_api_failure_skips_lines_silently(tmp_path, caplog):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(1, stderr=b"HTTP 403: rate limited")
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
        caplog.at_level(logging.DEBUG, logger="harness.runners.regret_review"),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_find_introducing_prs_malformed_pulls_json_skips_lines(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, b"this is not json")
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []


def test_find_introducing_prs_blame_failure_skips_only_that_range(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old a\n+new a\n@@ -9,1 +9,1 @@\n-old b\n+new b\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            if cmd[3] == "5,5":
                return _run(1, stderr=b"fatal: cannot blame")
            return _run(0, b"bbb222bbb222 (bob 2026-01-02 11:00:00 +0000   9) old b\n")
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 8}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(), _config(), str(tmp_path), {})
    assert [(h.old_start, h.old_end, h.introducing_sha) for h in hunks] == [(9, 9, "bbb222bbb222")]


def test_find_introducing_prs_merge_base_failure_blames_origin_base(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(1, stderr=b"fatal: no common ancestor")
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 7}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        hunks = find_introducing_prs(_pr(), _config(), str(tmp_path), {})
    assert len(hunks) == 1
    blame_cmd = _cmd_calls(mock_run, "git")[2]
    assert blame_cmd == ["git", "blame", "-L", "5,5", "origin/main", "--", "src/foo.py"]


def test_find_introducing_prs_no_changed_files_does_nothing(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=[]),
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    mock_run.assert_not_called()


def test_find_introducing_prs_no_base_branch_does_nothing(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value=""),
        patch("harness.runners.regret_review.get_changed_files") as mock_files,
    ):
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []
    mock_run.assert_not_called()
    mock_files.assert_not_called()


def test_find_introducing_prs_expired_budget_does_nothing(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        assert find_introducing_prs(_pr(), _config(timeout=0), str(tmp_path), {}) == []
    mock_run.assert_not_called()


def test_find_introducing_prs_timed_out_call_stops_sequence_fail_open(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        raise subprocess.TimeoutExpired(cmd, 1)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        # The diff succeeds, then the budget runs out during the next call: no partial
        # finding is produced.
        assert find_introducing_prs(_pr(), _config(), str(tmp_path), {}) == []


def test_find_introducing_prs_passes_remaining_budget_as_call_timeout(tmp_path):
    diff = "--- a/src/foo.py\n+++ b/src/foo.py\n@@ -5,1 +5,1 @@\n-old\n+new\n"
    blame = "abc123def456 (alice 2026-01-01 10:00:00 +0000   5) old\n"
    seen_timeouts: list[int] = []

    def side_effect(cmd, cwd, env, timeout, check=False):
        seen_timeouts.append(timeout)
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        if cmd[0] == "git" and cmd[1] == "merge-base":
            return _run(0, b"mb123mb123mb123mb123mb123\n")
        if cmd[0] == "git" and cmd[1] == "blame":
            return _run(0, blame.encode())
        if cmd[0] == "gh":
            return _run(0, json.dumps([{"number": 7}]).encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.get_pr_base_branch", return_value="main"),
        patch("harness.runners.regret_review.get_changed_files", return_value=["src/foo.py"]),
    ):
        find_introducing_prs(_pr(), _config(timeout=300), str(tmp_path), {})
    # Every call's timeout is the remaining budget (≤ 300, strictly decreasing in
    # elapsed time), never a fresh per-call constant.
    assert seen_timeouts and all(0 < t <= 300 for t in seen_timeouts)
    assert seen_timeouts == sorted(seen_timeouts, reverse=True)
