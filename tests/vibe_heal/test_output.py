"""Tests for output.py safe print helpers."""

from unittest.mock import patch

import pytest

from dotharness.vibe_heal.output import bold, bold_cyan, cyan, dim, error, info, success, warn


@pytest.fixture(autouse=True)
def _mock_console():
    """Patch the shared console for all tests in this module."""
    with patch("dotharness.vibe_heal.output.console") as mock:
        yield mock


@pytest.mark.parametrize(
    "fn,msg,expected",
    [
        (dim, "File [bad]", "[dim]File \\[bad][/dim]"),
        (success, "[green]spoofed[/green]", "[green]\\[green]spoofed\\[/green][/green]"),
        (warn, "Could not delete [project-key]", "[yellow]Could not delete \\[project-key][/yellow]"),
        (error, "Failed: [/path/to/file]", "[red]Failed: \\[/path/to/file][/red]"),
        (info, "AI tool: [claude-code]", "[blue]AI tool: \\[claude-code][/blue]"),
        (bold_cyan, "Processing [file]", "[bold cyan]Processing \\[file][/bold cyan]"),
        (cyan, "Status [red] alert", "[cyan]Status \\[red] alert[/cyan]"),
        (bold, "Header [red] text", "[bold]Header \\[red] text[/bold]"),
    ],
)
def test_helper_escapes_markup_tags(_mock_console, fn, msg, expected):
    fn(msg)
    _mock_console.print.assert_called_once_with(expected)


@pytest.mark.parametrize(
    "fn,msg,expected",
    [
        # [3] and [1] are not valid Rich markup tags — escape leaves them as-is
        (cyan, "Found [3] items", "[cyan]Found [3] items[/cyan]"),
        (bold, "Header [1]", "[bold]Header [1][/bold]"),
    ],
)
def test_helper_passes_through_non_tag_brackets(_mock_console, fn, msg, expected):
    fn(msg)
    _mock_console.print.assert_called_once_with(expected)


def test_plain_strings_pass_through(_mock_console):
    dim("No special chars here")
    _mock_console.print.assert_called_once_with("[dim]No special chars here[/dim]")
