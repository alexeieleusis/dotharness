"""Prose review of a plan document via the shared backend layer.

Must not be called from async code: ``launch_sync`` uses ``asyncio.run``. Neither argv, env nor prompt text is logged.
"""

from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path

from dotharness.ai_backend import ClaudeOptions, OpencodeOptions, launch_sync
from dotharness.git_utils import git_toplevel
from dotharness.spec_prism_flow.config import ProseReviewConfig

# Not either backend's own 1800-second full-build-cycle default: a prose review is a
# single-file copy-edit, not a full agent build cycle.
DEFAULT_PROSE_REVIEW_TIMEOUT_SECONDS = 900  # 15 minutes

_SKILL_RELATIVE_PATH = "skills/asd-ste100/SKILL.md"

logger = logging.getLogger(__name__)


def _skill_path() -> Path:
    return Path(str(resources.files("dotharness") / _SKILL_RELATIVE_PATH))


def _build_prompt(skill_path: Path, relative_target: Path) -> str:
    return (
        f"Read {skill_path} and apply it, in Strict mode, to {relative_target}.\n\n"
        f"Edit {relative_target} in place using your own file-editing tools. Do not print "
        "the rewritten text back as your response instead of editing the file -- the "
        "skill's own default behavior of returning rewritten text and nothing else does "
        "not apply here; there is no human to paste it back in.\n\n"
        "Preserve the document's meaning, its section structure, and every code "
        "identifier, CLI command, file path, and quoted name exactly, per the skill's "
        "own 'Code and names' rule.\n\n"
        "Do not add, remove, or reorder sections, and do not add any commentary about "
        "the changes you made."
    )


def review_document(
    cfg: ProseReviewConfig,
    target_path: Path,
    *,
    timeout: int = DEFAULT_PROSE_REVIEW_TIMEOUT_SECONDS,
) -> None:
    """Review `target_path` in place using the asd-ste100 Strict-mode skill, via the `cfg.backend` CLI harness.

    No-op when `cfg.enabled` is False. Never raises: any failure is logged as a warning and swallowed. The backend
    runs in the target's git toplevel; no retry, no cleanup."""
    if not cfg.enabled:
        return

    try:
        resolved_target = target_path.resolve()
        target_toplevel = git_toplevel(resolved_target.parent)
        relative_target = resolved_target.relative_to(target_toplevel.resolve())

        skill_path = _skill_path()
        if not skill_path.exists():
            logger.warning("prose review skipped for %s: skill file not found at %s", target_path, skill_path)
            return

        prompt = _build_prompt(skill_path, relative_target)
        if cfg.backend == "opencode":
            # Deliberately no auto=True: asymmetric with the Claude options.
            options: ClaudeOptions | OpencodeOptions = OpencodeOptions(standalone=True, opencode_dir=target_toplevel)
        else:
            options = ClaudeOptions(skip_permissions=True, disable_slash_commands=True)
        result = launch_sync(
            cfg.backend,
            prompt,
            cwd=target_toplevel,
            timeout=timeout,
            max_retries=0,
            backend_options=options,
        )
        if result.timed_out:
            logger.warning("prose review of %s timed out after %ss", target_path, timeout)
        if result.not_found:
            logger.warning("prose review of %s failed: %s backend executable not found", target_path, cfg.backend)
        if result.returncode != 0:
            logger.warning("prose review of %s exited with code %s", target_path, result.returncode)
    except Exception as exc:
        logger.warning("prose review of %s failed: %s", target_path, exc)
