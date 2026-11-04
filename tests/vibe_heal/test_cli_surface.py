"""Pin the vibe_heal CLI surface: command names, option names, short flags and defaults.

The expectation below is recorded from the original vibe-heal CLI. Renaming an option,
dropping a short flag or changing a default must fail this test.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from dotharness.vibe_heal.cli import app

runner = CliRunner()

# command -> {parameter name: (option/argument names, default, kind)}
_ENV = ("--env-file",), None, "option"
_VERBOSE = ("--verbose", "-v"), False, "option"
_BASE_BRANCH = ("--base-branch", "-b"), "origin/main", "option"
_MAX_ITERATIONS = ("--max-iterations", "-i"), 10, "option"
_PATTERN = ("--pattern", "-p"), None, "option"
_AI_TOOL = ("--ai-tool",), None, "option"
_DRY_RUN = ("--dry-run",), False, "option"
_MIN_SEVERITY = ("--min-severity",), None, "option"

EXPECTED: dict[str, dict[str, tuple[tuple[str, ...], Any, str]]] = {
    "cleanup": {
        "base_branch": _BASE_BRANCH,
        "max_iterations": _MAX_ITERATIONS,
        "file_patterns": _PATTERN,
        "ai_tool": _AI_TOOL,
        "env_file": _ENV,
        "verbose": _VERBOSE,
    },
    "cleanup-pr": {
        "base_branch": _BASE_BRANCH,
        "max_iterations": _MAX_ITERATIONS,
        "file_patterns": _PATTERN,
        "min_severity": _MIN_SEVERITY,
        "include_main_duplications": (("--include-main-duplications",), False, "option"),
        "dry_run": _DRY_RUN,
        "ai_tool": _AI_TOOL,
        "env_file": _ENV,
        "verbose": _VERBOSE,
    },
    "config": {
        "env_file": _ENV,
    },
    "convert-report": {
        "input_file": (("input_file",), None, "argument"),
        "output_file": (("output_file",), None, "argument"),
    },
    "debug-issues": {
        "file_path": (("file_path",), None, "argument"),
        "env_file": _ENV,
        "limit": (("--limit", "-n"), 10, "option"),
    },
    "dedupe": {
        "file_path": (("file_path",), None, "argument"),
        "dry_run": _DRY_RUN,
        "max_duplications": (("--max-duplications", "-n"), None, "option"),
        "ai_tool": _AI_TOOL,
        "env_file": _ENV,
        "verbose": _VERBOSE,
    },
    "dedupe-branch": {
        "base_branch": _BASE_BRANCH,
        "max_iterations": _MAX_ITERATIONS,
        "file_patterns": _PATTERN,
        "ai_tool": _AI_TOOL,
        "env_file": _ENV,
        "verbose": _VERBOSE,
    },
    "fix": {
        "file_path": (("file_path",), None, "argument"),
        "dry_run": _DRY_RUN,
        "max_issues": (("--max-issues", "-n"), None, "option"),
        "min_severity": _MIN_SEVERITY,
        "ai_tool": _AI_TOOL,
        "env_file": _ENV,
        "verbose": _VERBOSE,
    },
    "prune-projects": {
        "older_than": (("--older-than",), 60, "option"),
        "dry_run": _DRY_RUN,
        "yes": (("--yes", "-y"), False, "option"),
        "env_file": _ENV,
        "verbose": _VERBOSE,
    },
    "review": {
        "post": (("--post",), False, "option"),
        "dry_run": _DRY_RUN,
        "pr_number": (("--pr",), None, "option"),
        "base_branch": (("--base-branch", "-b"), None, "option"),
        "file_patterns": _PATTERN,
        "report_file": (("--report-file",), None, "option"),
        "env_file": _ENV,
        "verbose": _VERBOSE,
        "coverage": (("--coverage",), False, "option"),
        "baseline": (("--baseline",), False, "option"),
    },
    "version": {},
}


def _actual_surface() -> dict[str, dict[str, tuple[tuple[str, ...], Any, str]]]:
    group = typer.main.get_command(app)
    commands = getattr(group, "commands", {})
    return {
        name: {p.name or "": (tuple(p.opts), p.default, p.param_type_name) for p in command.params}
        for name, command in sorted(commands.items())
    }


def _plain(text: str) -> str:
    """Strip ANSI escapes and collapse whitespace so Rich panel layout does not matter."""
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _squash(text: str) -> str:
    """Drop ANSI escapes, Rich box-drawing characters and all whitespace (Rich wraps long option names)."""
    return re.sub(r"[\s\u2500-\u257f]+", "", re.sub(r"\x1b\[[0-9;]*m", "", text))


def test_command_names_match_recorded_expectation() -> None:
    assert sorted(_actual_surface()) == sorted(EXPECTED)


def test_option_names_short_flags_and_defaults_match_recorded_expectation() -> None:
    assert _actual_surface() == EXPECTED


def test_completion_is_disabled() -> None:
    output = _squash(runner.invoke(app, ["--help"]).output)
    assert "--install-completion" not in output
    assert "--show-completion" not in output


def test_app_help_lists_every_command_and_names_dotharness_repository() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    output = _plain(result.output)
    for name in EXPECTED:
        assert name in output
    assert "https://github.com/alexeieleusis/dotharness" in output
    assert "alexeieleusis/vibe-heal" not in output


@pytest.mark.parametrize("command", sorted(EXPECTED))
def test_command_help_lists_recorded_options(command: str) -> None:
    result = runner.invoke(app, [command, "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0
    output = _squash(result.output)
    for names, _default, kind in EXPECTED[command].values():
        if kind != "option":
            continue
        for name in names:
            assert _squash(name) in output, f"{command} --help does not list {name}"


def test_module_help_equals_script_help() -> None:
    env = {**os.environ, "COLUMNS": "120", "NO_COLOR": "1", "TERM": "dumb"}
    module = subprocess.run(
        [sys.executable, "-m", "dotharness.vibe_heal", "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    script_code = "import sys; sys.argv[0] = 'vibe_heal'; from dotharness.cli import vibe_heal; vibe_heal()"
    script = subprocess.run(
        [sys.executable, "-c", script_code, "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        cwd=Path(__file__).parent,
    )

    assert module.returncode == 0
    assert script.returncode == 0

    def normalise(text: str) -> str:
        # Only the program name in the usage line differs between the two entry points.
        return re.sub(r"Usage: .*? \[OPTIONS\]", "Usage: PROG [OPTIONS]", _plain(text))

    assert normalise(module.stdout) == normalise(script.stdout)


def test_version_command_reports_package_version() -> None:
    from importlib.metadata import version

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"vibe_heal version {version('dotharness')}"
