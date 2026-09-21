from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import click

logger = logging.getLogger(__name__)


class HandoffError(Exception):
    pass


def _copy_to_clipboard(text: str) -> None:
    try:
        proc = subprocess.run(["pbcopy"], input=text.encode(), capture_output=True)  # noqa: S607
    except FileNotFoundError:
        logger.warning("pbcopy not found on PATH; skipping clipboard copy")
        return
    if proc.returncode != 0:
        logger.warning("pbcopy exited %d: %s", proc.returncode, proc.stderr.decode("utf-8", errors="replace"))


def _copy_to_tmux_buffer(text: str) -> None:
    try:
        proc = subprocess.run(["tmux", "load-buffer", "-"], input=text.encode(), capture_output=True)  # noqa: S607
    except FileNotFoundError:
        logger.warning("tmux not found on PATH; skipping tmux buffer copy")
        return
    if proc.returncode != 0:
        logger.warning("tmux load-buffer exited %d: %s", proc.returncode, proc.stderr.decode("utf-8", errors="replace"))


def run_handoff(
    prompt_text: str,
    workspace_dir: Path,
    stage_name: str,
    output_filename: str | None = None,
) -> str:
    prompt_path = workspace_dir / f"{stage_name}_prompt.md"
    prompt_path.write_text(prompt_text)

    output_path = workspace_dir / (output_filename or f"{stage_name}_output.md")

    click.echo(f"Prompt written to: {prompt_path}")
    click.echo(f"Expected output at: {output_path}")

    _copy_to_clipboard(str(prompt_path))
    if os.environ.get("TMUX"):
        _copy_to_tmux_buffer(str(prompt_path))

    while not click.confirm("Agent finished writing output? "):
        pass

    if not output_path.exists():
        raise HandoffError(f"Expected output file not found: {output_path}")  # noqa: TRY003

    return output_path.read_text()
