from __future__ import annotations

import json
from pathlib import Path

MANIFEST_FILENAME = "init_manifest.json"


class ManifestError(Exception):
    pass


def manifest_path(workspace_dir: Path) -> Path:
    return workspace_dir / MANIFEST_FILENAME


def load_manifest(workspace_dir: Path) -> dict:
    path = manifest_path(workspace_dir)
    if not path.exists():
        raise ManifestError(f"Manifest not found: {path}; run 'plan init' first")  # noqa: TRY003
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ManifestError(f"Manifest is not valid JSON: {path}") from e  # noqa: TRY003
    if "brief" not in manifest:
        raise ManifestError(f"Manifest is missing required 'brief' key: {path}")  # noqa: TRY003
    return manifest


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
