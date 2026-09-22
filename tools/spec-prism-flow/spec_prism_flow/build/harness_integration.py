from __future__ import annotations

from pathlib import Path

from spec_prism_flow.build.errors import CommandError, run_subprocess
from spec_prism_flow.config import ReviewConfig

# Phase 01's ReviewConfig has no timeout field, so this mirrors
# claude_backend.DEFAULT_TIMEOUT_SECONDS: a module-level default every public function
# accepts as a `timeout` keyword, overridable per call. Same order of magnitude as
# ClaudeBackend's own agent-invocation timeout, since a self-review/address-comments
# cycle is itself a full agent run under the hood.
DEFAULT_HARNESS_TIMEOUT_SECONDS = 1800


class HarnessCommandError(CommandError):
    """A `harness run` subprocess exited non-zero (or timed out)."""


def run(config: ReviewConfig, clone: Path, subcommand: str, *, timeout: int = DEFAULT_HARNESS_TIMEOUT_SECONDS) -> str:
    """Invoke `uv run --project <config.tool_dir> <config.command> run --config
    <config.harness_config> <subcommand>` in `clone`, returning stdout. Raises
    HarnessCommandError on non-zero exit or timeout.

    Unlike self_review/address_comments, `run` does not check `config.enabled` --
    it's the low-level subprocess primitive they're both built on, and always
    executes when called directly.
    """
    cmd = [
        "uv",
        "run",
        "--project",
        str(config.tool_dir),
        config.command,
        "run",
        "--config",
        str(config.harness_config),
        subcommand,
    ]
    return run_subprocess(cmd, cwd=clone, timeout=timeout, error_cls=HarnessCommandError).stdout


def self_review(config: ReviewConfig, clone: Path, *, timeout: int = DEFAULT_HARNESS_TIMEOUT_SECONDS) -> str:
    """Safe to call unconditionally: short-circuits to `""` (no subprocess call) when
    `config.enabled` is False, so callers don't need to guard every call site
    themselves."""
    if not config.enabled:
        return ""
    return run(config, clone, "self-review", timeout=timeout)


def address_comments(config: ReviewConfig, clone: Path, *, timeout: int = DEFAULT_HARNESS_TIMEOUT_SECONDS) -> str:
    """Safe to call unconditionally: short-circuits to `""` (no subprocess call) when
    `config.enabled` is False, so callers don't need to guard every call site
    themselves."""
    if not config.enabled:
        return ""
    return run(config, clone, "address-comments", timeout=timeout)
