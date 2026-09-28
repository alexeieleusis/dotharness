import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from harness.config import HarnessConfig, HarnessSection, RegretReviewConfig, RepoConfig
from harness.runners.common import REGRET_REVIEW_MARKER, count_diff_lines
from harness.runners.regret_review import (
    IntroducingHunk,
    RegretCandidate,
    RegretFinding,
    _blamed_shas,
    _hunk_line_ranges,
    _line_within_hunk_ranges,
    _removed_modified_ranges,
    build_regret_comment_body,
    build_regret_judgment_prompt,
    find_introducing_prs,
    find_regret_candidates,
    parse_regret_judgment,
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
        "# Previously flagged review comments\n"
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
    assert body.count("# Previously flagged review comments") == 1
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


# --- phase 04: comment fetch + diff-hunk matching ---


def _hunk(**overrides) -> IntroducingHunk:
    base = {
        "path": "src/foo.py",
        "old_start": 5,
        "old_end": 5,
        "new_start": 5,
        "new_end": 5,
        "introducing_sha": "abc123def456",
        "introducing_pr_number": 7,
    }
    base.update(overrides)
    return IntroducingHunk(**base)


def _inline_comment(**overrides) -> dict:
    base = {
        "type": "inline",
        "id": 456,
        "author": "alice",
        "path": "src/foo.py",
        "line": 6,
        "side": "RIGHT",
        "body": "This edge case isn't handled.",
        "diff_hunk": "@@ -5,3 +5,3 @@\n-old a\n+new a\n old b",
        "url": "https://github.com/acme/repo/pull/7#discussion_r456",
        "replies": [],
    }
    base.update(overrides)
    return base


def _introducing_commit_diff(header: str, body: str = "-old\n+new\n") -> str:
    return f"diff --git a/src/foo.py b/src/foo.py\n--- a/src/foo.py\n+++ b/src/foo.py\n{header}\n{body}"


def test_hunk_line_ranges_inclusive_ranges_and_empty_sides():
    diff = "@@ -5,3 +5,3 @@\n@@ -8,0 +9,2 @@\n@@ -12,1 +14,0 @@\n@@ -5 +5 @@\n"
    assert _hunk_line_ranges(diff) == [
        (5, 7, 5, 7),
        (8, 7, 9, 10),  # pure addition: empty old side
        (12, 12, 14, 13),  # pure deletion: empty new side
        (5, 5, 5, 5),
    ]


def test_line_within_hunk_ranges_either_side_only():
    ranges = [(5, 7, 5, 7), (8, 7, 9, 10), (12, 12, 14, 13)]
    assert _line_within_hunk_ranges(5, ranges)
    assert _line_within_hunk_ranges(7, ranges)
    assert _line_within_hunk_ranges(9, ranges)  # new side of the pure addition
    assert _line_within_hunk_ranges(12, ranges)  # old side of the pure deletion
    assert not _line_within_hunk_ranges(4, ranges)
    assert not _line_within_hunk_ranges(8, ranges)  # inside no range: the addition's old side is empty
    assert not _line_within_hunk_ranges(14, ranges)  # inside no range: the deletion's new side is empty


def test_find_regret_candidates_happy_path_hunk_tolerant_not_exact_line(tmp_path):
    # The blamed change is line 5 only, but the introducing commit's hunk spans 5-7:
    # a comment at line 6 is in the same hunk and therefore a candidate.
    hunk = _hunk()
    diff = _introducing_commit_diff("@@ -5,3 +5,3 @@", "-old a\n+new a\n old b\n")

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment()]) as mock_fetch,
    ):
        candidates = find_regret_candidates([hunk], _config(), str(tmp_path), {})
    assert mock_fetch.call_count == 1
    assert mock_fetch.call_args.args[0] == 7  # the introducing PR, not the current one
    assert _cmd_calls(mock_run, "git") == [
        ["git", "diff", "abc123def456^", "abc123def456", "--unified=0", "--", "src/foo.py"]
    ]
    assert candidates == [
        RegretCandidate(
            comment_id=456,
            comment_body="This edge case isn't handled.",
            comment_author="alice",
            comment_diff_hunk="@@ -5,3 +5,3 @@\n-old a\n+new a\n old b",
            comment_url="https://github.com/acme/repo/pull/7#discussion_r456",
            hunk=hunk,
        )
    ]


def test_find_regret_candidates_comment_outside_hunk_window_not_a_candidate(tmp_path):
    diff = _introducing_commit_diff("@@ -5,3 +5,3 @@", "-old a\n+new a\n old b\n")
    comments = [
        _inline_comment(id=1, line=30),  # right file, far outside the 5-7 window
        _inline_comment(id=2, path="src/bar.py"),  # right line, wrong file
    ]

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=comments),
    ):
        assert find_regret_candidates([_hunk()], _config(), str(tmp_path), {}) == []


def test_find_regret_candidates_issue_and_review_types_never_candidates(tmp_path):
    diff = _introducing_commit_diff("@@ -5,3 +5,3 @@", "-old a\n+new a\n old b\n")
    comments = [
        _inline_comment(type="issue"),  # location-shaped, but the wrong type
        {
            "type": "review",
            "id": "review-777",
            "author": "bob",
            "state": "CHANGES_REQUESTED",
            "body": "whole-PR nits",
            "url": "https://github.com/acme/repo/pull/7#pullrequestreview-777",
        },
    ]

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=comments),
    ):
        assert find_regret_candidates([_hunk()], _config(), str(tmp_path), {}) == []


def test_find_regret_candidates_dedupes_fetch_and_diff_and_yields_a_pair_per_range(tmp_path):
    # Two blamed ranges in one file, both attributed to the same commit/PR, both inside
    # that commit's single 5-9 hunk: one fetch, one hunk-diff, one candidate per range.
    hunk_a = _hunk(old_start=5, old_end=5, new_start=5, new_end=5)
    hunk_b = _hunk(old_start=9, old_end=9, new_start=9, new_end=9)
    diff = _introducing_commit_diff(
        "@@ -5,5 +5,5 @@",
        "-old a\n+new a\n-old b\n+new b\n-old c\n+new c\n-old d\n+new d\n-old e\n+new e\n",
    )

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment()]) as mock_fetch,
    ):
        candidates = find_regret_candidates([hunk_a, hunk_b], _config(), str(tmp_path), {})
    assert mock_fetch.call_count == 1  # one fetch for the one unique introducing PR
    assert len(_cmd_calls(mock_run, "git")) == 1  # one hunk-diff for the one unique (commit, file)
    assert [c.hunk for c in candidates] == [hunk_a, hunk_b]
    assert all(c.comment_id == 456 for c in candidates)


def test_find_regret_candidates_same_commit_in_two_prs_fetches_each_once(tmp_path):
    hunk_a = _hunk(introducing_pr_number=7)
    hunk_b = _hunk(old_start=9, old_end=9, new_start=9, new_end=9, introducing_pr_number=8)
    diff = _introducing_commit_diff(
        "@@ -5,5 +5,5 @@",
        "-old a\n+new a\n-old b\n+new b\n-old c\n+new c\n-old d\n+new d\n-old e\n+new e\n",
    )

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    def fetch_side_effect(pr_number, script_path, wdir, env):
        if pr_number == 7:
            return [_inline_comment(line=5)]
        if pr_number == 8:
            return [_inline_comment(id=789, line=7, url="https://github.com/acme/repo/pull/8#discussion_r789")]
        msg = f"unexpected PR: {pr_number}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect) as mock_run,
        patch("harness.runners.regret_review.fetch_pr_comments", side_effect=fetch_side_effect) as mock_fetch,
    ):
        candidates = find_regret_candidates([hunk_a, hunk_b], _config(), str(tmp_path), {})
    assert [c.args[0] for c in mock_fetch.call_args_list] == [7, 8]
    assert len(_cmd_calls(mock_run, "git")) == 1  # the shared (commit, file) is diffed once
    assert [(c.hunk, c.comment_id) for c in candidates] == [(hunk_a, 456), (hunk_b, 789)]


def test_find_regret_candidates_fetch_failure_drops_only_that_pr(tmp_path, caplog):
    hunk_a = _hunk(introducing_pr_number=7)
    hunk_b = _hunk(old_start=9, old_end=9, new_start=9, new_end=9, introducing_pr_number=8)
    diff = _introducing_commit_diff(
        "@@ -5,5 +5,5 @@",
        "-old a\n+new a\n-old b\n+new b\n-old c\n+new c\n-old d\n+new d\n-old e\n+new e\n",
    )

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    def fetch_side_effect(pr_number, script_path, wdir, env):
        if pr_number == 7:
            return []  # models fetch_pr_comments' failure/empty outcome
        return [_inline_comment(id=789, line=7)]

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", side_effect=fetch_side_effect),
        caplog.at_level(logging.DEBUG, logger="harness.runners.regret_review"),
    ):
        candidates = find_regret_candidates([hunk_a, hunk_b], _config(), str(tmp_path), {})
    assert [c.hunk for c in candidates] == [hunk_b]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_find_regret_candidates_hunk_diff_failure_drops_only_that_commit(tmp_path, caplog):
    hunk_a = _hunk(introducing_sha="aaa111aaa111", introducing_pr_number=7)
    hunk_b = _hunk(
        introducing_sha="bbb222bbb222",
        old_start=9,
        old_end=9,
        new_start=9,
        new_end=9,
        introducing_pr_number=8,
    )
    diff = _introducing_commit_diff("@@ -9,1 +9,1 @@", "-old b\n+new b\n")

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            if "aaa111aaa111" in cmd[2]:
                return _run(1, stderr=b"fatal: unknown revision 'aaa111aaa111^'")  # e.g. a root commit
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment(line=9, id=789)]),
        caplog.at_level(logging.DEBUG, logger="harness.runners.regret_review"),
    ):
        candidates = find_regret_candidates([hunk_a, hunk_b], _config(), str(tmp_path), {})
    assert [c.hunk for c in candidates] == [hunk_b]
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_find_regret_candidates_comment_on_old_side_of_introducing_hunk(tmp_path):
    # The introducing commit replaced lines 5-6 with lines 5-8; a comment anchored to
    # old line 5 (LEFT side, a line the change deleted) is still in the same hunk.
    hunk = _hunk(old_start=5, old_end=6, new_start=5, new_end=4)  # the current PR deletes them
    diff = _introducing_commit_diff("@@ -5,2 +5,4 @@", "-old a\n-old b\n+new a\n+new b\n+new c\n")

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment(line=5, side="LEFT")]),
    ):
        candidates = find_regret_candidates([hunk], _config(), str(tmp_path), {})
    assert [c.hunk for c in candidates] == [hunk]


def test_find_regret_candidates_pure_addition_introducing_commit_matches_new_side_only(tmp_path):
    # The introducing commit added lines 5-7 (its old side is empty): a comment at
    # new line 7 matches; one at line 4 — outside the hunk on both sides — does not.
    hunk = _hunk(old_start=5, old_end=7, new_start=5, new_end=5)
    diff = _introducing_commit_diff("@@ -4,0 +5,3 @@", "+new a\n+new b\n+new c\n")
    comments = [_inline_comment(id=1, line=4), _inline_comment(id=2, line=7)]

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=comments),
    ):
        candidates = find_regret_candidates([hunk], _config(), str(tmp_path), {})
    assert [c.comment_id for c in candidates] == [2]


def test_find_regret_candidates_comment_with_no_line_never_a_candidate(tmp_path):
    # A stale inline comment whose line and original_line are both missing has no
    # locatable position: it can never match.
    diff = _introducing_commit_diff("@@ -5,3 +5,3 @@", "-old a\n+new a\n old b\n")

    def side_effect(cmd, cwd, env, timeout, check=False):
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment(line=None)]),
    ):
        assert find_regret_candidates([_hunk()], _config(), str(tmp_path), {}) == []


def test_find_regret_candidates_empty_input_does_nothing(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.fetch_pr_comments") as mock_fetch,
    ):
        assert find_regret_candidates([], _config(), str(tmp_path), {}) == []
    mock_run.assert_not_called()
    mock_fetch.assert_not_called()


def test_find_regret_candidates_expired_budget_does_nothing(tmp_path):
    with (
        patch("harness.runners.regret_review.run_cmd") as mock_run,
        patch("harness.runners.regret_review.fetch_pr_comments") as mock_fetch,
    ):
        assert find_regret_candidates([_hunk()], _config(timeout=0), str(tmp_path), {}) == []
    mock_run.assert_not_called()
    mock_fetch.assert_not_called()


def test_find_regret_candidates_timed_out_diff_stops_sequence_fail_open(tmp_path):
    # The fetch succeeds, then the hunk-diff times out: the sequence stops and no
    # partial candidate set is produced.

    def side_effect(cmd, cwd, env, timeout, check=False):
        raise subprocess.TimeoutExpired(cmd, 1)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment()]),
    ):
        assert find_regret_candidates([_hunk()], _config(), str(tmp_path), {}) == []


def test_find_regret_candidates_passes_remaining_budget_as_call_timeout(tmp_path):
    hunk = _hunk()
    diff = _introducing_commit_diff("@@ -5,3 +5,3 @@", "-old a\n+new a\n old b\n")
    seen_timeouts: list[int] = []

    def side_effect(cmd, cwd, env, timeout, check=False):
        seen_timeouts.append(timeout)
        if cmd[0] == "git" and cmd[1] == "diff":
            return _run(0, diff.encode())
        msg = f"unexpected command: {cmd}"
        raise AssertionError(msg)

    with (
        patch("harness.runners.regret_review.run_cmd", side_effect=side_effect),
        patch("harness.runners.regret_review.fetch_pr_comments", return_value=[_inline_comment()]),
    ):
        find_regret_candidates([hunk, hunk], _config(timeout=300), str(tmp_path), {})
    # Every git call's timeout is the remaining budget (≤ 300, decreasing in elapsed
    # time), never a fresh per-call constant.
    assert seen_timeouts and all(0 < t <= 300 for t in seen_timeouts)
    assert seen_timeouts == sorted(seen_timeouts, reverse=True)


# --- phase 05: backend judgment prompt and response parsing ---


def _candidate(**overrides) -> RegretCandidate:
    base = {
        "comment_id": 456,
        "comment_body": "This edge case isn't handled.",
        "comment_author": "alice",
        "comment_diff_hunk": "@@ -5,3 +5,3 @@\n-old a\n+new a\n old b",
        "comment_url": "https://github.com/acme/repo/pull/7#discussion_r456",
        "hunk": _hunk(),
    }
    base.update(overrides)
    return RegretCandidate(**base)


def _candidates(count: int) -> list[RegretCandidate]:
    return [
        _candidate(
            comment_id=456 + i,
            comment_body=f"comment {i}",
            comment_author=f"author{i}",
            comment_url=f"https://github.com/acme/repo/pull/{7 + i}#discussion_r{456 + i}",
            hunk=_hunk(
                path=f"src/f{i}.py",
                old_start=5 + i,
                old_end=5 + i,
                new_start=6 + i,
                new_end=6 + i,
                introducing_pr_number=7 + i,
            ),
        )
        for i in range(count)
    ]


def test_build_regret_judgment_prompt_orders_instructions_diff_candidates_and_trailer():
    prompt = build_regret_judgment_prompt(
        "INSTRUCTIONS",
        [_candidate(), _candidate(comment_id=789)],
        "DIFF-SECTIONS",
        _pr(),
        42,
        "acme/repo",
        "deadbeef",
        None,
        None,
    )
    assert prompt.index("INSTRUCTIONS") == 0
    assert prompt.index("DIFF-SECTIONS") < prompt.index("## Regret Candidates")
    assert prompt.index("## Regret Candidates") < prompt.index("### Candidate 1")
    assert prompt.index("### Candidate 1") < prompt.index("### Candidate 2")
    assert prompt.index("### Candidate 2") < prompt.index("PR URL: https://github.com/acme/repo/pull/42")
    assert "PR number: 42" in prompt
    assert "Repo: acme/repo" in prompt
    assert "Commit: deadbeef" in prompt


def test_build_regret_judgment_prompt_carries_blamed_region_comment_and_old_diff_per_candidate():
    c1 = _candidate()
    c2 = _candidate(
        comment_id=789,
        comment_body="Off by one in the retry counter.",
        comment_author="bob",
        comment_diff_hunk="@@ -9,1 +9,1 @@\n-old retry\n+new retry",
        comment_url="https://github.com/acme/repo/pull/8#discussion_r789",
        hunk=_hunk(
            path="src/bar.py",
            old_start=9,
            old_end=9,
            new_start=9,
            new_end=9,
            introducing_sha="bbb222bbb222",
            introducing_pr_number=8,
        ),
    )
    prompt = build_regret_judgment_prompt(
        "INSTRUCTIONS", [c1, c2], "DIFFS", _pr(), 42, "acme/repo", "deadbeef", None, None
    )
    block1 = prompt[prompt.index("### Candidate 1") : prompt.index("### Candidate 2")]
    block2 = prompt[prompt.index("### Candidate 2") :]
    assert "src/foo.py lines 5-5 (pre-fix), replaced by lines 5-5 (post-fix)" in block1
    assert "from PR #7, by alice" in block1
    assert "> This edge case isn't handled." in block1
    assert "@@ -5,3 +5,3 @@\n-old a\n+new a\n old b" in block1
    assert "src/bar.py lines 9-9 (pre-fix), replaced by lines 9-9 (post-fix)" in block2
    assert "from PR #8, by bob" in block2
    assert "> Off by one in the retry counter." in block2
    assert "@@ -9,1 +9,1 @@\n-old retry\n+new retry" in block2
    # Candidate 1's content does not leak into candidate 2's block.
    assert "This edge case isn't handled." not in block2


def test_build_regret_judgment_prompt_notes_pure_deletion_region():
    candidate = _candidate(hunk=_hunk(old_start=12, old_end=12, new_start=14, new_end=13))
    prompt = build_regret_judgment_prompt(
        "INSTRUCTIONS", [candidate], "DIFFS", _pr(), 42, "acme/repo", "deadbeef", None, None
    )
    assert "src/foo.py lines 12-12 (pre-fix); deleted by this fix" in prompt


def test_build_regret_judgment_prompt_empty_diff_hunk_renders_fallback():
    candidate = _candidate(comment_diff_hunk="")
    prompt = build_regret_judgment_prompt(
        "INSTRUCTIONS", [candidate], "DIFFS", _pr(), 42, "acme/repo", "deadbeef", None, None
    )
    assert "(no diff anchor)" in prompt


def test_build_regret_judgment_prompt_trailer_carries_description_and_vibe_heal_when_present():
    prompt = build_regret_judgment_prompt(
        "INSTRUCTIONS",
        [_candidate()],
        "DIFFS",
        _pr(),
        42,
        "acme/repo",
        "deadbeef",
        "## PR Description\n**Title:** fix bug",
        "sonar context",
    )
    assert "## PR Description\n**Title:** fix bug" in prompt
    assert "## Static Analysis\nsonar context" in prompt
    bare = build_regret_judgment_prompt(
        "INSTRUCTIONS", [_candidate()], "DIFFS", _pr(), 42, "acme/repo", "deadbeef", None, None
    )
    assert "## Static Analysis" not in bare
    assert "**Title:**" not in bare


def test_build_regret_judgment_prompt_multiline_body_stays_one_blockquote():
    candidate = _candidate(comment_body="line one\nline two")
    prompt = build_regret_judgment_prompt(
        "INSTRUCTIONS", [candidate], "DIFFS", _pr(), 42, "acme/repo", "deadbeef", None, None
    )
    assert "> line one\n> line two" in prompt


def test_parse_regret_judgment_all_yes_one_finding_per_candidate_in_candidate_order():
    candidates = _candidates(3)
    response = "CANDIDATE 1: YES — first\nCANDIDATE 2: YES — second\nCANDIDATE 3: YES — third\n"
    findings = parse_regret_judgment(response, candidates)
    assert [f.path for f in findings] == ["src/f0.py", "src/f1.py", "src/f2.py"]
    assert [f.line for f in findings] == [6, 7, 8]  # the post-image's first line (new_start)
    assert [f.introducing_pr_number for f in findings] == [7, 8, 9]
    assert [f.comment_id for f in findings] == [456, 457, 458]
    assert [f.comment_body for f in findings] == ["comment 0", "comment 1", "comment 2"]
    assert [f.comment_author for f in findings] == ["author0", "author1", "author2"]
    assert [f.comment_url for f in findings] == [c.comment_url for c in candidates]


def test_parse_regret_judgment_all_no_yields_no_findings():
    response = "CANDIDATE 1: NO — unrelated\nCANDIDATE 2: NO — different root cause\n"
    assert parse_regret_judgment(response, _candidates(2)) == []


def test_parse_regret_judgment_mixed_verdicts_keep_only_yes():
    response = (
        "CANDIDATE 1: NO — unrelated\nCANDIDATE 2: YES — predicted exactly this bug\nCANDIDATE 3: NO — style point\n"
    )
    findings = parse_regret_judgment(response, _candidates(3))
    assert [f.comment_id for f in findings] == [457]


def test_parse_regret_judgment_out_of_range_indices_ignored_but_all_real_candidates_still_required():
    response = (
        "CANDIDATE 0: YES — below the range\n"
        "CANDIDATE 1: YES — in range\n"
        "CANDIDATE 2: NO — in range\n"
        "CANDIDATE 3: NO — in range\n"
        "CANDIDATE 4: YES — above the range\n"
    )
    findings = parse_regret_judgment(response, _candidates(3))
    assert [f.comment_id for f in findings] == [456]


def test_parse_regret_judgment_no_candidates_never_a_finding():
    assert parse_regret_judgment("CANDIDATE 1: YES — nobody to map to", []) == []


def test_parse_regret_judgment_malformed_lines_discarded_valid_lines_kept():
    response = (
        "CANDIDATE 1: MAYBE — not a verdict\n"
        "candidate 2: yes — lowercase does not match the contract\n"
        "CANDIDATE 2 YES — missing the colon\n"
        "CANDIDATE 3: NO — discarded\n"
        "CANDIDATE 2: YES — the valid verdict line\n"
        "CANDIDATE 1: YES — the valid verdict line for candidate 1\n"
        "I confirmed one candidate in total.\n"
    )
    findings = parse_regret_judgment(response, _candidates(3))
    assert [f.comment_id for f in findings] == [456, 457]


def test_parse_regret_judgment_missing_rationale_is_not_a_valid_verdict_line():
    assert parse_regret_judgment("CANDIDATE 1: YES\n", _candidates(1)) == []


def test_parse_regret_judgment_response_omitting_a_candidate_yields_no_findings():
    response = "CANDIDATE 1: YES — in range\n"
    assert parse_regret_judgment(response, _candidates(3)) == []


def test_parse_regret_judgment_garbage_response_yields_no_findings():
    response = "I cannot tell from this diff whether the old comment predicted this bug. Let me reconsider..."
    assert parse_regret_judgment(response, _candidates(3)) == []


def test_parse_regret_judgment_ignores_prose_around_verdict_lines():
    response = (
        "After reviewing the diff against each candidate:\n"
        "CANDIDATE 1: YES — the comment predicted exactly this failure.\n"
        "I hope this is useful.\n"
    )
    assert [f.comment_id for f in parse_regret_judgment(response, _candidates(1))] == [456]


def test_parse_regret_judgment_pure_deletion_falls_back_to_old_start():
    candidate = _candidate(hunk=_hunk(old_start=12, old_end=12, new_start=14, new_end=13))
    findings = parse_regret_judgment("CANDIDATE 1: YES — the deleted line", [candidate])
    assert (findings[0].path, findings[0].line) == ("src/foo.py", 12)


def test_parse_regret_judgment_repeated_verdict_for_one_candidate_invalidates_response():
    response = "CANDIDATE 1: YES — first line\nCANDIDATE 1: NO — later, contradictory line\n"
    assert parse_regret_judgment(response, _candidates(1)) == []


def test_parse_regret_judgment_yes_on_review_typed_candidate_discarded():
    candidates = [
        _candidate(comment_id="review-777"),
        _candidate(comment_id=456),
    ]
    response = "CANDIDATE 1: YES — review-level comment\nCANDIDATE 2: YES — inline comment\n"
    findings = parse_regret_judgment(response, candidates)
    assert [f.comment_id for f in findings] == [456]


def test_parse_regret_judgment_tolerates_indented_verdict_lines():
    response = "    CANDIDATE 1: YES — indented verdict line\n"
    assert [f.comment_id for f in parse_regret_judgment(response, _candidates(1))] == [456]
