"""Tests for AI tool utilities (run_command stays for the gh helper; prompts go through the shared launcher)."""

import sys

import pytest

from dotharness.vibe_heal.ai_tools.utils import CommandResult, run_command


class TestRunCommand:
    """Tests for run_command."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        """A zero exit is a success with captured stdout."""
        result = await run_command([sys.executable, "-c", "print('hi')"])

        assert result == CommandResult(success=True, stdout="hi\n", stderr="", exit_code=0)

    @pytest.mark.asyncio
    async def test_failure_captures_stderr(self) -> None:
        """A non-zero exit is a failure with captured stderr."""
        result = await run_command([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"])

        assert result.success is False
        assert result.exit_code == 3
        assert result.stderr == "bad"
