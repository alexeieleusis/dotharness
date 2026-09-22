from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from spec_prism_flow.build.errors import CommandError
from spec_prism_flow.config import VibeHealConfig

# Mirrors harness_integration.DEFAULT_HARNESS_TIMEOUT_SECONDS -- Phase 01's
# VibeHealConfig has no timeout field either, so this is a module-level default every
# public function accepts as a `timeout` keyword, overridable per call.
DEFAULT_VIBE_HEAL_TIMEOUT_SECONDS = 1800


class VibeHealCommandError(CommandError):
    """A `vibe-heal review` subprocess exited non-zero (or timed out)."""


class SonarScannerNotFoundError(RuntimeError):
    """`sonar-scanner` isn't on PATH. Raised by `scan` before it ever shells out to
    `vibe-heal review`, so a missing scanner fails with a clear message instead of
    surfacing as an opaque non-zero exit from deep inside vibe-heal's own default-path
    SonarQube-config resolution."""


@lru_cache(maxsize=1)
def _sonar_scanner_on_path() -> str | None:
    return shutil.which("sonar-scanner")


def _run(config: VibeHealConfig, clone: Path, *args: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Shared subprocess primitive for `scan`/`post`: `uv run --project
    <config.tool_dir> <config.command> review <*args>` in `clone`. Both callers pass
    `--report-file`/`--env-file` through `*args` -- omitting either risks an unhandled
    `sys.exit(1)` on vibe-heal's own default-path SonarQube-config resolution failure,
    a correctness requirement inherited from the source this phase ports, not a
    stylistic choice."""
    cmd = ["uv", "run", "--project", str(config.tool_dir), config.command, "review", *args]
    try:
        result = subprocess.run(  # noqa: S603
            cmd, cwd=clone, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise VibeHealCommandError(cmd, -1, f"timed out after {timeout}s: {exc.stderr or ''}") from exc
    if result.returncode != 0:
        raise VibeHealCommandError(cmd, result.returncode, result.stderr)
    return result


def scan(
    config: VibeHealConfig,
    clone: Path,
    report_path: Path,
    env_path: Path,
    *,
    timeout: int = DEFAULT_VIBE_HEAL_TIMEOUT_SECONDS,
) -> dict | None:
    """Run `vibe-heal review --report-file <report_path> --env-file <env_path>` in
    `clone`, then read back and parse `report_path` as the scan's result. Returns
    `None` and makes no subprocess call when `config.enabled` is False. `report_path`/
    `env_path` are mandatory, caller-supplied paths -- VibeHealConfig carries no
    default location for either, and every invocation must pass them explicitly (see
    `_run`'s docstring for why)."""
    if not config.enabled:
        return None
    if _sonar_scanner_on_path() is None:
        raise SonarScannerNotFoundError("sonar-scanner not found on PATH")  # noqa: TRY003
    cmd = _run(config, clone, "--report-file", str(report_path), "--env-file", str(env_path), timeout=timeout).args
    try:
        return json.loads(report_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise VibeHealCommandError(list(cmd), 0, f"report file missing or unparsable after exit 0: {exc}") from exc


def post(
    config: VibeHealConfig,
    clone: Path,
    report_path: Path,
    env_path: Path,
    *,
    timeout: int = DEFAULT_VIBE_HEAL_TIMEOUT_SECONDS,
) -> None:
    """Run `vibe-heal review --report-file <report_path> --env-file <env_path> --post`
    in `clone`, telling vibe-heal to post its findings. A no-op when `config.enabled`
    is False. Carries no deduplication logic of its own -- callers must call
    `should_post` first and only call `post` when it returns True."""
    if not config.enabled:
        return
    _run(config, clone, "--report-file", str(report_path), "--env-file", str(env_path), "--post", timeout=timeout)


@dataclass(frozen=True)
class Fingerprint:
    """Identifies a single vibe-heal issue for cross-cycle dedup. `(rule, file, line)`
    is the narrowest triple that's still stable across re-scans of the same PR state --
    vibe-heal's own issue ids, if any, aren't documented anywhere in this codebase as
    stable across runs, and an issue's message text can be reworded between vibe-heal
    versions without the underlying finding changing, so neither is included.

    There's no Fingerprint type or report-parsing shape anywhere else in this
    codebase; `report`'s shape below is designed from scratch against §7.2's stated
    fields (`rule`/`file`/`line`/`on_changed_line`), as the smallest SARIF-like JSON
    shape that satisfies them -- vibe-heal's actual --report-file format isn't
    documented in this checkout."""

    rule: str
    file: str
    line: int


def fingerprints_from_report(report: dict) -> set[Fingerprint]:
    """`report` is vibe-heal's --report-file JSON, parsed: `{"issues": [{"rule": str,
    "file": str, "line": int, "message": str, "on_changed_line": bool}, ...]}`. Only
    `on_changed_line=True` issues are included -- vibe-heal 422s when asked to post an
    inline comment on a trailing-context line, so an issue on such a line is never a
    postable fingerprint, only visible via the raw report."""
    return {
        Fingerprint(rule=issue["rule"], file=issue["file"], line=issue["line"])
        for issue in report.get("issues", [])
        if issue.get("on_changed_line")
    }


def should_post(current: set[Fingerprint], already_posted: set[Fingerprint]) -> bool:
    """True only if this cycle surfaced a fingerprint never posted before."""
    return bool(current - already_posted)


def diff_fingerprints(previous: set[Fingerprint], current: set[Fingerprint]) -> tuple[int, int]:
    """(opened, resolved) counts between two scan cycles."""
    opened = len(current - previous)
    resolved = len(previous - current)
    return opened, resolved
