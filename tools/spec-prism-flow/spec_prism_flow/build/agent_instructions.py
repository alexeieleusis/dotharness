from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_instructions_file(instructions: str, *, tmp_dir: Path | None = None) -> Path:
    """Write `instructions` to a fresh temp `.md` file and return its path.

    Callers are responsible for deleting the file (typically in a `finally`) once
    the backend process has consumed it. `tmp_dir`, when given, is created if
    missing -- pass a backend-specific directory (as ClaudeBackend does) or leave it
    unset to fall back to the bare system tempdir (as OpencodeBackend does).
    """
    if tmp_dir is not None:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, path_str = tempfile.mkstemp(suffix=".md", dir=tmp_dir, prefix="spec_prism_flow_")
    tmp_path = Path(path_str)
    os.close(fd)
    try:
        tmp_path.write_text(instructions, encoding="utf-8")
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path


def read_prompt(tmp_path: Path) -> str:
    return f"Read {tmp_path} and follow the instructions exactly."
