"""Tests for AI tool utilities."""

from pathlib import Path
from unittest.mock import patch

import pytest

from dotharness.vibe_heal.ai_tools.utils import build_file_prompt, temp_prompt_file, write_prompt_file


class TestWritePromptFile:
    """Tests for write_prompt_file."""

    @pytest.mark.asyncio
    async def test_writes_prompt_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the file contains the given prompt content."""
        monkeypatch.chdir(tmp_path)

        path = await write_prompt_file("do the thing")

        assert path.read_text(encoding="utf-8") == "do the thing"

    @pytest.mark.asyncio
    async def test_creates_file_in_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the file is created in the current working directory."""
        monkeypatch.chdir(tmp_path)

        path = await write_prompt_file("prompt")

        assert path.parent == tmp_path

    @pytest.mark.asyncio
    async def test_default_suffix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the default suffix is .txt."""
        monkeypatch.chdir(tmp_path)

        path = await write_prompt_file("prompt")

        assert path.suffix == ".txt"

    @pytest.mark.asyncio
    async def test_custom_suffix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that a custom suffix is honored."""
        monkeypatch.chdir(tmp_path)

        path = await write_prompt_file("prompt", suffix=".md")

        assert path.suffix == ".md"

    @pytest.mark.asyncio
    async def test_unlinks_file_on_write_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the temp file is removed if writing the prompt fails."""
        monkeypatch.chdir(tmp_path)

        with (
            patch("dotharness.vibe_heal.ai_tools.utils.aiofiles.open", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            await write_prompt_file("prompt")

        assert list(tmp_path.iterdir()) == []


class TestTempPromptFile:
    """Tests for temp_prompt_file."""

    @pytest.mark.asyncio
    async def test_file_exists_and_readable_during_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that the file exists and is readable inside the context block."""
        monkeypatch.chdir(tmp_path)

        async with temp_prompt_file("do the thing") as path:
            assert path.exists()
            assert path.read_text(encoding="utf-8") == "do the thing"

    @pytest.mark.asyncio
    async def test_cleans_up_after_normal_exit(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the file is deleted after the context block exits normally."""
        monkeypatch.chdir(tmp_path)

        async with temp_prompt_file("prompt") as path:
            pass

        assert not path.exists()

    @pytest.mark.asyncio
    async def test_cleans_up_on_exception(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that the file is deleted even when the body raises."""
        monkeypatch.chdir(tmp_path)

        with pytest.raises(RuntimeError):
            async with temp_prompt_file("prompt") as path:
                assert path.exists()
                raise RuntimeError("boom")

        assert not path.exists()


class TestBuildFilePrompt:
    """Tests for build_file_prompt."""

    def test_references_filename_and_instructs_to_follow(self) -> None:
        """Test the wrapper instruction references the file by basename."""
        result = build_file_prompt(Path("/some/dir/vibeheal_abc123.txt"))

        assert result == 'Read "vibeheal_abc123.txt" and follow the instructions in that file exactly.'
