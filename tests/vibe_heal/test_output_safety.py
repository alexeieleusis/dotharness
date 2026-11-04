"""Structural test: no direct markup interpolation outside output.py.

Enforces that all styled terminal output goes through the helpers in
vibe_heal/output.py, which escape dynamic values internally. This prevents
MarkupError from user data (file paths, exception messages, scanner output)
containing Rich markup characters like `[` and `]`.
"""

import re
from pathlib import Path


def test_no_markup_fstring_outside_output() -> None:
    """Fail if any file (other than output.py) interpolates variables inside markup tags.

    Pattern caught: console.print(f"[style]{variable}[/style]")
    Pattern caught: console.rule(f"[style]{variable}[/style]")
    Pattern caught: console.log(f"[style]{variable}[/style]")
    Pattern caught: console.out(f"[style]{variable}[/style]")
    Pattern allowed: console.print("[style]literal text[/style]")
    Pattern allowed: console.print(f"plain text {variable}")
    """
    src_dir = Path(__file__).resolve().parents[2] / "src" / "dotharness" / "vibe_heal"
    # Match f-strings passed to any markup-interpolating Console method that
    # contain BOTH a `[` (markup tag opener) and a `{` (f-string interpolation),
    # in either order. Lookaheads avoid requiring a specific left-to-right ordering.
    pattern = re.compile(
        r"(?:self\.)?console\.(?:print|rule|log|out)\(f[\"']"
        r"(?=[^\"']*\[)"  # lookahead: [ appears somewhere in the string
        r"(?=[^\"']*\{)"  # lookahead: { appears somewhere in the string
    )
    violations: list[str] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        if py_file.name == "output.py":
            continue
        for lineno, line in enumerate(py_file.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if pattern.search(stripped):
                rel = py_file.relative_to(src_dir.parent.parent)
                violations.append(f"  {rel}:{lineno}: {stripped}")

    assert not violations, (
        "Use helpers from dotharness.vibe_heal.output (dim, error, warn, success, info, "
        "cyan, bold_cyan) instead of console.<method>(f'[markup]{dynamic}[/markup]').\n"
        "Violations:\n" + "\n".join(violations)
    )
