"""Guard: the migrated plan package must not grow the build stage, backends or process-group killing."""

from pathlib import Path

import dotharness.spec_prism_flow as spec_prism_flow_pkg

FORBIDDEN = (
    "spec_prism_flow.build",
    "ClaudeBackend",
    "OpencodeBackend",
    "subprocess.Popen",
    "os.killpg",
)


def _package_sources() -> list[Path]:
    return sorted(Path(next(iter(spec_prism_flow_pkg.__path__))).rglob("*.py"))


def test_package_sources_are_found():
    assert any(p.name == "decompose.py" for p in _package_sources())


def test_no_module_contains_forbidden_names():
    offenders = [
        f"{path.name}: {token}"
        for path in _package_sources()
        for token in FORBIDDEN
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
