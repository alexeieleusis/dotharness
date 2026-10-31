"""Archive directory helpers: resolve ``[archive] dir`` and check it is safe."""

# ruff: noqa: TRY003

from __future__ import annotations

from pathlib import Path

from sisyphus.config import ConfigError


def _workspace_root(workspace_dir: str | None) -> Path:
    if not workspace_dir:
        raise ConfigError("[project].workspace_dir is not set")
    return Path(workspace_dir).expanduser()


def resolve_archive_dir(dir: str, workspace_dir: str | None) -> Path:  # noqa: A002
    """Resolve ``[archive] dir``: relative values sit under ``workspace_dir``; absolute ones are kept.

    Read-only: nothing is created. Raises ``ConfigError`` if ``dir`` is relative and
    ``workspace_dir`` is unset.
    """
    p = Path(dir).expanduser()
    if p.is_absolute():
        return p
    return _workspace_root(workspace_dir) / dir


def check_archive_dir(archive_dir: Path, workspace_dir: str | None) -> None:
    """Raise ``ConfigError`` if ``archive_dir`` equals or contains the workspace root."""
    workspace = _workspace_root(workspace_dir).resolve()
    archive = archive_dir.resolve()
    if archive == workspace or archive in workspace.parents:
        raise ConfigError(f"archive dir {archive} must not equal or contain the workspace {workspace}")
