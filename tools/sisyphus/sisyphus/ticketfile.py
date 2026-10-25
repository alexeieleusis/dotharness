"""On-disk format of ``file`` tracker tickets: ``<dir>/<ref>.md`` with a leading ``---`` front-matter block.

The single owner of the layout (ref rule, extension, directory expansion, front-matter syntax); the file tracker in
``pick`` and the ticket handling in ``intake`` both depend on this module and not on each other.
"""

# ruff: noqa: TRY003
from __future__ import annotations

import re
from pathlib import Path

from sisyphus.config import WorkItemSource

TICKET_SUFFIX = ".md"

_FILE_REF_RE = re.compile(r"[A-Za-z0-9._-]+")
_FRONT_MATTER_FENCE = "---"


def valid_file_ref(ref: str) -> bool:
    """True if ``ref`` is a safe ticket-file stem: ``[A-Za-z0-9._-]+`` and not made only of dots."""
    return _FILE_REF_RE.fullmatch(ref) is not None and ref.strip(".") != ""


def ticket_dir(source: WorkItemSource) -> Path:
    """The ``~``-expanded ticket directory of a ``file`` source. Raises ``ValueError`` if ``dir`` is not set."""
    if not source.dir:
        raise ValueError("[source.work_item].dir is not set")
    return Path(source.dir).expanduser()


def ticket_path(source: WorkItemSource, ref: str) -> Path:
    """``<dir>/<ref>.md``. Raises ``ValueError`` for an invalid ref, so ``../x`` cannot leave the directory."""
    if not valid_file_ref(ref):
        raise ValueError(f"invalid ticket reference {ref!r}")
    return ticket_dir(source) / f"{ref}{TICKET_SUFFIX}"


def ref_from_filename(name: str) -> str:
    """The ref a ticket file name stands for (the name without its ``.md`` suffix)."""
    return name.removesuffix(TICKET_SUFFIX)


def parse_front_matter(text: str) -> dict[str, str]:
    """Key/value pairs of the leading ``---`` block (each line split on its first ``:``).

    Raises ``ValueError`` for a missing opening or closing fence. The body below the block is never read.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FRONT_MATTER_FENCE:
        raise ValueError("missing opening ---")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == _FRONT_MATTER_FENCE:
            return fields
        key, sep, value = line.partition(":")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    raise ValueError("missing closing ---")
