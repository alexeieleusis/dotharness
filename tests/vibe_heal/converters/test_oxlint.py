"""Tests for oxlint to ESLint format converter."""

from __future__ import annotations

from typing import Any

from dotharness.vibe_heal.converters.oxlint import convert_oxlint_to_eslint


def _make_diagnostic(
    filename: str = "src/foo.ts",
    message: str = "test message",
    code: str = "no-unused-vars",
    severity: str = "warning",
    line: int = 10,
    column: int = 4,
    length: int = 5,
    labels: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal oxlint diagnostic dict."""
    if labels is None:
        labels = [{"span": {"offset": 0, "length": length, "line": line, "column": column}}]
    return {
        "filename": filename,
        "message": message,
        "code": code,
        "severity": severity,
        "labels": labels,
    }


class TestConvertOxlintToEslint:
    """Tests for convert_oxlint_to_eslint."""

    def test_groups_diagnostics_by_filename(self) -> None:
        data = {
            "diagnostics": [
                _make_diagnostic(filename="src/a.ts"),
                _make_diagnostic(filename="src/b.ts"),
                _make_diagnostic(filename="src/a.ts"),
            ]
        }
        result = convert_oxlint_to_eslint(data)
        assert len(result) == 2
        paths = {f["filePath"] for f in result}
        assert paths == {"src/a.ts", "src/b.ts"}
        a_file = next(f for f in result if f["filePath"] == "src/a.ts")
        assert len(a_file["messages"]) == 2

    def test_all_message_keys_present(self) -> None:
        result = convert_oxlint_to_eslint({"diagnostics": [_make_diagnostic()]})
        msg = result[0]["messages"][0]
        assert set(msg.keys()) == {"ruleId", "severity", "message", "line", "column"}

    def test_no_labels_falls_back_to_1_1(self) -> None:
        data = {"diagnostics": [_make_diagnostic(labels=[])]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["line"] == 1
        assert msg["column"] == 1

    def test_span_null_falls_back_to_1_1_1_1(self) -> None:
        data = {"diagnostics": [_make_diagnostic(labels=[{"span": None}])]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["line"] == 1
        assert msg["column"] == 1

    def test_span_without_length_still_extracts_position(self) -> None:
        data = {"diagnostics": [_make_diagnostic(labels=[{"span": {"line": 5, "column": 2}}])]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["line"] == 5
        assert msg["column"] == 3  # 2 + 1

    def test_rule_id_eslint_plugin_prefix_stripped(self) -> None:
        data = {"diagnostics": [_make_diagnostic(code="eslint-plugin-react-hooks(exhaustive-deps)")]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["ruleId"] == "react-hooks/exhaustive-deps"

    def test_rule_id_plain_prefix(self) -> None:
        data = {"diagnostics": [_make_diagnostic(code="unicorn(prefer-string-slice)")]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["ruleId"] == "unicorn/prefer-string-slice"

    def test_rule_id_hyphenated_namespace(self) -> None:
        data = {"diagnostics": [_make_diagnostic(code="react-hooks(rules-of-hooks)")]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["ruleId"] == "react-hooks/rules-of-hooks"

    def test_rule_id_no_parens_kept_verbatim(self) -> None:
        data = {"diagnostics": [_make_diagnostic(code="no-unused-vars")]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["ruleId"] == "no-unused-vars"

    def test_rule_id_nested_parens_kept_verbatim(self) -> None:
        data = {"diagnostics": [_make_diagnostic(code="foo(bar(baz))")]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["ruleId"] == "foo(bar(baz))"

    def test_severity_error_maps_to_2(self) -> None:
        data = {"diagnostics": [_make_diagnostic(severity="error")]}
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["severity"] == 2

    def test_severity_non_error_maps_to_1(self) -> None:
        for sev in ("warning", "info", "hint", ""):
            data = {"diagnostics": [_make_diagnostic(severity=sev)]}
            msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
            assert msg["severity"] == 1, f"Expected 1 for severity={sev!r}"

    def test_empty_diagnostics_returns_empty_list(self) -> None:
        assert convert_oxlint_to_eslint({"diagnostics": []}) == []

    def test_per_file_error_and_warning_counts(self) -> None:
        data = {
            "diagnostics": [
                _make_diagnostic(severity="error"),
                _make_diagnostic(severity="warning"),
            ]
        }
        file_obj = convert_oxlint_to_eslint(data)[0]
        assert file_obj["errorCount"] == 1
        assert file_obj["warningCount"] == 1

    def test_fixable_counts_always_zero(self) -> None:
        file_obj = convert_oxlint_to_eslint({"diagnostics": [_make_diagnostic()]})[0]
        assert file_obj["fixableErrorCount"] == 0
        assert file_obj["fixableWarningCount"] == 0

    def test_source_is_none(self) -> None:
        file_obj = convert_oxlint_to_eslint({"diagnostics": [_make_diagnostic()]})[0]
        assert file_obj["source"] is None

    def test_message_comes_from_message_field_not_help(self) -> None:
        diag = _make_diagnostic(message="the real message")
        diag["help"] = "do this instead"
        msg = convert_oxlint_to_eslint({"diagnostics": [diag]})[0]["messages"][0]
        assert msg["message"] == "the real message"

    def test_line_and_column_from_span(self) -> None:
        # span column=4 (0-indexed) → column_out=5 (1-indexed for ESLint)
        data = {
            "diagnostics": [_make_diagnostic(labels=[{"span": {"offset": 0, "length": 11, "line": 38, "column": 4}}])]
        }
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["line"] == 38
        assert msg["column"] == 5  # 4 + 1

    def test_column_zero_indexed_conversion(self) -> None:
        data = {
            "diagnostics": [_make_diagnostic(labels=[{"span": {"offset": 0, "length": 0, "line": 5, "column": 3}}])]
        }
        msg = convert_oxlint_to_eslint(data)[0]["messages"][0]
        assert msg["column"] == 4  # 3 + 1
