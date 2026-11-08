"""Tests for AI tool utilities (run_command stays for the gh helper; prompts go through the shared launcher)."""

import asyncio
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

    @pytest.mark.asyncio
    async def test_missing_executable_raises_file_not_found(self) -> None:
        """A launch failure still surfaces as an OSError subclass, as before the routing."""
        with pytest.raises(FileNotFoundError):
            await run_command(["dotharness-no-such-binary"])

    @pytest.mark.asyncio
    async def test_caller_deadline_cancels_and_kills(self) -> None:
        """An outer asyncio.timeout() still bounds the call (no own deadline)."""
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.2):
                await run_command([sys.executable, "-c", "import time; time.sleep(30)"])
