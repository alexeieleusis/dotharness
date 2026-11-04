"""Safe terminal output helpers for vibe-heal.

This module is the single place in vibe_heal where Rich markup strings are
constructed at runtime. All dynamic values are escaped before insertion so that
user data containing brackets (file paths, exception messages, scanner output)
can never trigger a Rich MarkupError.

Usage:
    from dotharness.vibe_heal.output import console, dim, success, warn, error, info, cyan, bold, bold_cyan

Do NOT call console.print(f"[markup]{dynamic}[/markup]") anywhere else in the
codebase. Use these helpers instead — they handle escaping internally.
"""

from rich.console import Console
from rich.markup import escape as _esc

console = Console()


def _print(tag: str, msg: str) -> None:
    console.print(f"[{tag}]{_esc(msg)}[/{tag}]")


def dim(msg: str) -> None:
    """Print msg in muted/dim style. msg is auto-escaped."""
    _print("dim", msg)


def success(msg: str) -> None:
    """Print msg in green."""
    _print("green", msg)


def warn(msg: str) -> None:
    """Print msg in yellow."""
    _print("yellow", msg)


def error(msg: str) -> None:
    """Print msg in red."""
    _print("red", msg)


def info(msg: str) -> None:
    """Print msg in blue."""
    _print("blue", msg)


def cyan(msg: str) -> None:
    """Print msg in cyan."""
    _print("cyan", msg)


def bold(msg: str) -> None:
    """Print msg in bold."""
    _print("bold", msg)


def bold_cyan(msg: str) -> None:
    """Print msg in bold cyan."""
    _print("bold cyan", msg)
