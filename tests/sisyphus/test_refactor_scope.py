"""Pure refactor scope helpers: ``scope_violations`` and ``parse_listed_edits``."""

from __future__ import annotations

from dotharness.sisyphus.orchestrator import parse_listed_edits, scope_violations

FILES = ["src/a.py"]
TESTS = ["tests/test_a.py"]


def _check(out: str, listed: set[str] | None = None) -> list[str]:
    return scope_violations(out, FILES, TESTS, listed or set())


def test_modified_unlisted_reported() -> None:
    assert _check("M\tsrc/other.py\n") == ["M src/other.py"]


def test_modified_listed_ok() -> None:
    assert _check("M\tsrc/other.py\n", {"src/other.py"}) == []


def test_modified_test_ok() -> None:
    assert _check("M\ttests/test_a.py\n") == []


def test_modified_file_ok() -> None:
    assert _check("M\tsrc/a.py\n") == []


def test_added_always_ok() -> None:
    assert _check("A\tsrc/x.py\nA\tsrc/a.py\nA\ttests/test_a.py\n") == []


def test_move_old_in_files() -> None:
    assert _check("D\tsrc/a.py\nA\tsrc/new/a.py\n") == []


def test_move_old_in_tests() -> None:
    assert _check("D\ttests/test_a.py\nA\ttests/new/test_a.py\n") == []


def test_move_old_listed() -> None:
    assert _check("D\tsrc/old.py\nA\tsrc/new.py\n", {"src/old.py"}) == []


def test_move_old_in_none() -> None:
    assert _check("D\tsrc/old.py\nA\tsrc/new.py\n") == ["D src/old.py"]


def test_deleted_unlisted_reported() -> None:
    assert _check("D\tsrc/other.py\n") == ["D src/other.py"]


def test_several_in_order_no_duplicates() -> None:
    out = "M\tsrc/z.py\nD\tsrc/y.py\nM\tsrc/z.py\nA\tsrc/n.py\n"
    assert _check(out) == ["M src/z.py", "D src/y.py"]


def test_empty_and_blank() -> None:
    assert _check("") == []
    assert _check("\n  \n\n") == []


def test_type_change_reported() -> None:
    assert _check("T\tsrc/other.py\n") == ["T src/other.py"]


def test_line_without_tab_skipped() -> None:
    assert _check("garbage\n") == []


def test_bullet_text_after_colon() -> None:
    assert parse_listed_edits("- Edits to existing files: a.py reason\n") == {"a.py"}


def test_backticked_path() -> None:
    assert parse_listed_edits("- Edits to existing files: `a.py`\n") == {"a.py"}


def test_dash_and_star_items() -> None:
    text = "- Edits to existing files:\n  - a.py\n  * b.py\n- Next:\n"
    assert parse_listed_edits(text) == {"a.py", "b.py"}


def test_indented_item() -> None:
    assert parse_listed_edits("- Edits to existing files:\n    - a.py\n") == {"a.py"}


def test_reason_ignored() -> None:
    assert parse_listed_edits("- Edits to existing files:\n  - a.py because reference line\n") == {"a.py"}


def test_none_yields_empty() -> None:
    assert parse_listed_edits("- Edits to existing files: none\n") == set()
    assert parse_listed_edits("- Edits to existing files:\n  - None.\n") == set()


def test_ends_at_next_top_level_bullet() -> None:
    text = "- Edits to existing files:\n  - a.py\n- Open questions:\n  - b.py\n"
    assert parse_listed_edits(text) == {"a.py"}


def test_ends_at_end_of_text() -> None:
    assert parse_listed_edits("- Edits to existing files:\n  - a.py\n  - b.py") == {"a.py", "b.py"}


def test_missing_bullet() -> None:
    assert parse_listed_edits("- Deviations from the plan: none\n  - a.py\n") == set()


def test_trailing_colon_on_path() -> None:
    assert parse_listed_edits("- Edits to existing files:\n  - `a.py`: reason\n") == {"a.py"}


def test_crlf_same_as_lf() -> None:
    lf = "- Edits to existing files:\n  - a.py\n  - `b.py`: why\n- Next:\n"
    assert parse_listed_edits(lf.replace("\n", "\r\n")) == parse_listed_edits(lf) == {"a.py", "b.py"}


def test_only_first_bullet_read() -> None:
    text = "- Edits to existing files:\n  - a.py\n- Edits to existing files:\n  - b.py\n"
    assert parse_listed_edits(text) == {"a.py"}


def test_several_items_on_one_line() -> None:
    text = (
        "- Deviations from the plan: none\n"
        "- Edits to existing files: src/app/main.py (import of the extracted module); "
        "src/app/routes.py (call site of the moved handler)\n"
    )
    assert parse_listed_edits(text) == {"src/app/main.py", "src/app/routes.py"}
