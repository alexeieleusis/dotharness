from __future__ import annotations

import json
from pathlib import Path

MANIFEST_FILENAME = "init_manifest.json"


def manifest_path(workspace_dir: Path) -> Path:
    return workspace_dir / MANIFEST_FILENAME


def build_manifest(
    brief_path: Path,
    code_path: Path | None,
    conventions_path: Path | None,
    links: list[str],
) -> dict:
    return {
        "brief": str(brief_path.resolve()),
        "code": str(code_path.resolve()) if code_path else None,
        "conventions": str(conventions_path.resolve()) if conventions_path else None,
        "links": list(links),
    }


def init_workspace(
    workspace_dir: Path,
    brief_path: Path,
    code_path: Path | None,
    conventions_path: Path | None,
    links: list[str],
) -> Path:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(brief_path, code_path, conventions_path, links)
    path = manifest_path(workspace_dir)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return path
