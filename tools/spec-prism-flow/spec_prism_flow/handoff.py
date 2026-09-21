from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import click

logger = logging.getLogger(__name__)


class HandoffError(Exception):
    pass


def _run_best_effort(cmd: list[str], text: str, tool_name: str) -> None:
    try:
        proc = subprocess.run(cmd, input=text.encode(), capture_output=True)  # noqa: S603
    except FileNotFoundError:
        logger.warning("%s not found on PATH; skipping copy", tool_name)
        return
    if proc.returncode != 0:
        logger.warning("%s exited %d: %s", tool_name, proc.returncode, proc.stderr.decode("utf-8", errors="replace"))


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

    _run_best_effort(["pbcopy"], str(prompt_path), "pbcopy")
    if os.environ.get("TMUX"):
        _run_best_effort(["tmux", "load-buffer", "-"], str(prompt_path), "tmux load-buffer")

    while not click.confirm("Agent finished writing output? "):
        continue

    if not output_path.exists():
        raise HandoffError(f"Expected output file not found: {output_path}")  # noqa: TRY003

    return output_path.read_text()
