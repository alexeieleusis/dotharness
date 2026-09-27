from harness.runners.common import REGRET_REVIEW_MARKER
from harness.runners.regret_review import RegretFinding, build_regret_comment_body


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
