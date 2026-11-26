"""Gate record: tells the pr_review merge runner which head SHA the build gate ran on.

Writes ``{"head_sha": "<sha>"}`` to the path from ``dotharness.paths.build_gate_record_path``. The write is
atomic and idempotent (overwriting is the normal path). Sisyphus must not import ``dotharness.pr_review``,
so the record location comes from the neutral ``dotharness.paths`` module.
"""

from __future__ import annotations

from pathlib import Path

from dotharness.fileio import atomic_write_json
from dotharness.paths import build_gate_record_path


def write_gate_record(repo_slug: str, pr_number: int, head_sha: str) -> Path:
    """Write the gate record for ``pr_number`` and return its path.

    An invalid ``repo_slug`` raises ``ValueError`` before anything is written; ``OSError`` propagates.
    ``head_sha`` is written as given (not validated).
    """
    path = build_gate_record_path(repo_slug, pr_number)
    atomic_write_json(path, {"head_sha": head_sha})
    return path
