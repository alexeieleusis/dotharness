"""Shared resolution of the default base branch: setting, then host default branch, then ``origin/main``."""

from dataclasses import dataclass
from typing import Literal

from dotharness.code_host import CodeHost, CodeHostError
from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.git.branch_analyzer import BranchAnalyzer

DEFAULT_BASE_BRANCH = "origin/main"
REMOTE_PREFIX = "origin/"

BaseSource = Literal["setting", "host", "fallback"]


@dataclass(frozen=True)
class BaseResolution:
    """The resolved default base ref and how it was reached.

    Attributes:
        ref: The ref to diff against (for example ``origin/develop``).
        source: ``setting`` (DOTHARNESS_BASE_BRANCH), ``host`` (default branch) or ``fallback`` (``origin/main``).
        host_read_failed: True when the host default branch was needed but could not be read (or was empty).
    """

    ref: str
    source: BaseSource
    host_read_failed: bool = False


def _short_name(value: str) -> str:
    """Return the short branch name, accepting a value that already carries the ``origin/`` prefix."""
    return value.removeprefix(REMOTE_PREFIX)


def _resolve_setting(name: str, branch_analyzer: BranchAnalyzer) -> str:
    """Return ``origin/<name>``, else the local branch ``<name>``, else ``origin/<name>`` again."""
    remote_ref = f"{REMOTE_PREFIX}{name}"
    if branch_analyzer.validate_branch_exists(remote_ref):
        return remote_ref
    if branch_analyzer.validate_branch_exists(name):
        return name
    return remote_ref


async def resolve_default_base(
    config: VibeHealConfig, code_host: CodeHost | None, branch_analyzer: BranchAnalyzer
) -> BaseResolution:
    """Resolve the default base: the setting, then the host default branch, then ``origin/main``.

    Callers apply an explicit flag (and any per-PR base) first and call this only when still needed. The host is
    read only when the setting is unset. ``code_host=None`` means no host is available, which is reported as a
    host read failure. Never raises for host failures and never logs: callers decide whether to warn, based on
    ``BaseResolution.host_read_failed``.
    """
    if config.base_branch:
        return BaseResolution(_resolve_setting(_short_name(config.base_branch), branch_analyzer), "setting")

    if code_host is None:
        return BaseResolution(DEFAULT_BASE_BRANCH, "fallback", host_read_failed=True)

    try:
        repo = await code_host.repository.current()
        default = await code_host.repository.default_branch(repo)
    except (CodeHostError, OSError):
        return BaseResolution(DEFAULT_BASE_BRANCH, "fallback", host_read_failed=True)

    if not default or not default.strip():
        return BaseResolution(DEFAULT_BASE_BRANCH, "fallback", host_read_failed=True)
    return BaseResolution(f"{REMOTE_PREFIX}{default.strip()}", "host")
