from __future__ import annotations

from pathlib import Path

import typer


class HandoffError(Exception):
    pass


def wait_for_confirmation(prompt: str) -> None:
    while not typer.confirm(prompt):
        continue


def run_handoff(
    prompt_text: str,
    workspace_dir: Path,
    stage_name: str,
    output_filename: str | None = None,
) -> str:
    prompt_path = workspace_dir / f"{stage_name}_prompt.md"
    prompt_path.write_text(prompt_text)

    output_path = workspace_dir / (output_filename or f"{stage_name}_output.md")
    output_path.unlink(missing_ok=True)

    typer.echo(f"Prompt written to: {prompt_path}")
    typer.echo(f"Expected output at: {output_path}")

    wait_for_confirmation("Agent finished writing output?")

    if not output_path.exists():
        raise HandoffError(f"Expected output file not found: {output_path}")  # noqa: TRY003

    return output_path.read_text()
