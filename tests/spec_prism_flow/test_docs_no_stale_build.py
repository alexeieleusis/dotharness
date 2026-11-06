"""G8: migrated docs must not present the unmigrated `build` command as usable."""

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs" / "spec_prism_flow"
STALE = re.compile(r"spec[-_]prism[-_]flow\s+build")


def test_no_stale_build_examples() -> None:
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(DOCS.glob("*.md"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if STALE.search(line) and "not migrated" not in line.lower()
    ]
    assert not offenders, offenders
