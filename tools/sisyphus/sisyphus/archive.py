"""Archive helpers: resolve ``[archive] dir``, list an item's files and move them into the archive."""

# ruff: noqa: TRY003

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from sisyphus.config import ConfigError, SisyphusConfig
from sisyphus.intake import SIDECAR_SUFFIX, IntakeError, resolve_brief_path
from sisyphus.orchestrator import plan_dir_name
from sisyphus.resume import USED_SUFFIX, answer_path
from sisyphus.runrecord import RunRecord
from sisyphus.stoprecord import stop_record_path


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


@dataclass
class ArchiveResult:
    """Outcome of :func:`archive_item` for one item.

    ``moved``: destination paths moved by this call, in move order (run record last).
    ``collision``: the first existing destination path that made the mover refuse the item; when set nothing
    was moved. ``warnings``: human-readable messages for the caller to print to stderr (missing brief or
    sidecar, unset brief template, source not deleted after a cross-filesystem copy).
    """

    item: str
    moved: list[Path] = field(default_factory=list)
    collision: Path | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def refused(self) -> bool:
        return self.collision is not None


def _collect(config: SisyphusConfig, rec: RunRecord, archive_dir: Path | None) -> tuple[list[Path], list[str]]:
    """Existing item files (run record last) and warnings about a brief that cannot be found."""
    rec_path = rec.path
    workspace_dir = config.project.workspace_dir
    workspace = Path(workspace_dir).expanduser() if workspace_dir else rec_path.parent
    candidates = [
        stop_record_path(rec_path),
        answer_path(rec_path),
        answer_path(rec_path).with_name(answer_path(rec_path).name + USED_SUFFIX),
        workspace / plan_dir_name(rec.item),
    ]
    warnings: list[str] = []
    try:
        brief = resolve_brief_path(config, rec.item)
    except IntakeError as exc:
        warnings.append(f"{rec.item}: brief not archived: {exc}")
    else:
        sidecar = brief.with_name(brief.name + SIDECAR_SUFFIX)
        for path, label in ((brief, "brief"), (sidecar, "brief sidecar")):
            if path.exists() or path.is_symlink():
                candidates.append(path)
            elif archive_dir is None or not (archive_dir / path.name).exists():
                warnings.append(f"{rec.item}: {label} not found: {path}")
    files = [p for p in candidates if p.exists() or p.is_symlink()]
    if rec_path.exists():
        files.append(rec_path)
    return files, warnings


def item_files(config: SisyphusConfig, rec: RunRecord) -> list[Path]:
    """Existing files of one item (stop record, answer files, brief and sidecar, ``plan-<n>/``), run record last.

    Missing paths are skipped. Digests, the legacy shared ``handoffs/`` and the legacy ``.stop.json`` suffix are
    not part of the set. Does not select items.
    """
    return _collect(config, rec, None)[0]


def _copy_then_delete(src: Path, dest: Path, warnings: list[str]) -> None:
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dest, symlinks=True)
    else:
        shutil.copy2(src, dest, follow_symlinks=False)
    try:
        if src.is_dir() and not src.is_symlink():
            shutil.rmtree(src)
        else:
            src.unlink()
    except OSError as exc:
        warnings.append(f"copied {src} to {dest} but could not remove the source: {exc}")


def _move(src: Path, dest: Path, warnings: list[str]) -> None:
    try:
        os.replace(src, dest)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy_then_delete(src, dest, warnings)


def archive_item(config: SisyphusConfig, rec: RunRecord, archive_dir: Path) -> ArchiveResult:
    """Move one item's files flat into ``archive_dir`` under their original names; never overwrites.

    Per (source, destination) pair: source absent and destination present means already moved (skipped);
    source present and destination present is a collision and refuses the whole item before anything moves
    (no rollback); source present and destination absent is moved. The run record moves last, so an
    interrupted run leaves the item in the workspace and a rerun completes it. A fully archived item is a no-op.
    Same filesystem uses ``os.replace``; on ``EXDEV`` the file or tree is copied, then the source deleted (a
    failed delete is a warning and the source is left). Other ``OSError`` propagate.
    """
    files, warnings = _collect(config, rec, archive_dir)
    result = ArchiveResult(item=rec.item, warnings=warnings)
    pairs = [(src, archive_dir / src.name) for src in files]
    for _, dest in pairs:
        if dest.exists() or dest.is_symlink():
            result.collision = dest
            return result
    if pairs:
        archive_dir.mkdir(parents=True, exist_ok=True)
    for src, dest in pairs:
        _move(src, dest, result.warnings)
        result.moved.append(dest)
    return result
