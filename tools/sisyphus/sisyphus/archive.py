"""Archive helpers: resolve ``[archive] dir``, list an item's files and move them into the archive."""

# ruff: noqa: TRY003

from __future__ import annotations

import contextlib
import errno
import filecmp
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


def _partial_path(dest: Path) -> Path:
    return dest.with_name(f".{dest.name}.partial")


def _remove_path(path: Path) -> None:
    """Remove a file, symlink or tree; missing paths are ignored."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with contextlib.suppress(OSError):  # best effort: a leftover partial is retried on the next run
            path.unlink(missing_ok=True)


def _copy_then_delete(src: Path, dest: Path, warnings: list[str]) -> None:
    # Copy to a temporary name, then rename: only a complete copy ever appears at ``dest``.
    partial = _partial_path(dest)
    _remove_path(partial)
    try:
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, partial, symlinks=True)
        else:
            shutil.copy2(src, partial, follow_symlinks=False)
        os.rename(partial, dest)  # same directory, so never EXDEV
    except BaseException:
        _remove_path(partial)
        raise
    _delete_source(src, dest, warnings)


def _same_content(src: Path, dest: Path) -> bool:
    """True if ``dest`` is a complete copy of ``src`` (file, symlink or tree)."""
    if src.is_symlink() or dest.is_symlink():
        return src.is_symlink() and dest.is_symlink() and os.readlink(src) == os.readlink(dest)
    if src.is_dir():
        if not dest.is_dir():
            return False
        cmp = filecmp.dircmp(src, dest)
        if cmp.left_only or cmp.right_only or cmp.funny_files:
            return False
        return all(_same_content(src / n, dest / n) for n in cmp.common)
    return dest.is_file() and filecmp.cmp(src, dest, shallow=False)


def _delete_source(src: Path, dest: Path, warnings: list[str]) -> None:
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
    source present and destination present with identical content is a leftover of a failed delete (the source is
    removed again); with different content it is a collision and refuses the whole item before anything moves
    (no rollback); source present and destination absent is moved. The run record moves last, so an
    interrupted run leaves the item in the workspace and a rerun completes it. A fully archived item is a no-op.
    Same filesystem uses ``os.replace``; on ``EXDEV`` the file or tree is copied, then the source deleted (a
    failed delete is a warning and the source is left); the copy goes to ``.<name>.partial`` first and is renamed
    when complete, so an existing destination is always whole and stale partials are cleaned on rerun. Other ``OSError`` propagate.
    """
    files, warnings = _collect(config, rec, archive_dir)
    result = ArchiveResult(item=rec.item, warnings=warnings)
    pairs = [(src, archive_dir / src.name) for src in files]
    for _, dest in pairs:
        _remove_path(_partial_path(dest))  # stale leftover of an interrupted cross-filesystem copy
    stale: list[tuple[Path, Path]] = []  # identical copy already at dest: a delete failed on an earlier run
    for src, dest in pairs:
        if dest.exists() or dest.is_symlink():
            if not _same_content(src, dest):
                result.collision = dest
                return result
            stale.append((src, dest))
    if pairs:
        archive_dir.mkdir(parents=True, exist_ok=True)
    for src, dest in pairs:
        if (src, dest) in stale:
            _delete_source(src, dest, result.warnings)
            continue
        _move(src, dest, result.warnings)
        result.moved.append(dest)
    return result
