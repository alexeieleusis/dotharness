"""Structural test: no direct markup interpolation outside output.py.

Enforces that all styled terminal output goes through the helpers in
vibe_heal/output.py, which escape dynamic values internally. This prevents
MarkupError from user data (file paths, exception messages, scanner output)
containing Rich markup characters like `[` and `]`.
"""

import ast
from pathlib import Path

_CONSOLE_METHODS = frozenset({"print", "rule", "log", "out"})


def _is_console(node: ast.expr) -> bool:
    """True for `console`, `self.console`, `x.console`, `_console`, etc."""
    if isinstance(node, ast.Name):
        return node.id.endswith("console")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("console")
    return False


def _is_markup_fstring(node: ast.expr) -> bool:
    """True if node is an f-string with an interpolation and a literal `[`."""
    if not isinstance(node, ast.JoinedStr):
        return False
    has_value = any(isinstance(v, ast.FormattedValue) for v in node.values)
    has_bracket = any(isinstance(v, ast.Constant) and isinstance(v.value, str) and "[" in v.value for v in node.values)
    return has_value and has_bracket


def _find_violations(source: str) -> list[int]:
    """Return line numbers of console calls passing a markup f-string."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _CONSOLE_METHODS
            and _is_console(node.func.value)
            and any(_is_markup_fstring(a) for a in node.args)
        ):
            lines.append(node.lineno)
    return lines


def test_detector_catches_known_shapes() -> None:
    """The AST check must flag wrapped calls and strings containing apostrophes."""
    assert _find_violations('console.print(\n    f"[red]{err}[/red]"\n)\n') == [1]
    assert _find_violations('console.print(f"[red]Can\'t open {path}[/red]")\n') == [1]
    assert _find_violations('self.console.rule(f"[b]{x}[/b]")\n') == [1]
    assert _find_violations('console.print("[red]literal[/red]")\n') == []
    assert _find_violations('console.print(f"plain {x}")\n') == []


def test_no_markup_fstring_outside_output() -> None:
    """Fail if any file (other than output.py) interpolates variables inside markup tags.

    Pattern caught: console.print(f"[style]{variable}[/style]")  (also multi-line)
    Pattern caught: console.rule/log/out with the same shape
    Pattern allowed: console.print("[style]literal text[/style]")
    Pattern allowed: console.print(f"plain text {variable}")
    """
    src_dir = Path(__file__).resolve().parents[2] / "src" / "dotharness" / "vibe_heal"
    assert src_dir.is_dir(), f"source dir not found: {src_dir}"
    scanned = 0
    violations: list[str] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        if py_file.name == "output.py":
            continue
        scanned += 1
        for lineno in _find_violations(py_file.read_text()):
            rel = py_file.relative_to(src_dir.parent.parent)
            violations.append(f"  {rel}:{lineno}")

    assert scanned > 0, f"no Python files scanned under {src_dir}"
    assert not violations, (
        "Use helpers from dotharness.vibe_heal.output (dim, error, warn, success, info, "
        "cyan, bold_cyan) instead of console.<method>(f'[markup]{dynamic}[/markup]').\n"
        "Violations:\n" + "\n".join(violations)
    )
