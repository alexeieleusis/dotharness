from __future__ import annotations

import logging
from pathlib import Path

from spec_prism_flow.build import git_ops
from spec_prism_flow.build.agent_runner import AgentBackend
from spec_prism_flow.build.claude_backend import ClaudeBackend
from spec_prism_flow.build.opencode_backend import OpencodeBackend
from spec_prism_flow.config import ProseReviewConfig

# Not either backend's own 1800-second full-build-cycle default: a prose review is a
# single-file copy-edit, not a full agent build cycle.
DEFAULT_PROSE_REVIEW_TIMEOUT_SECONDS = 900  # 15 minutes

_SKILL_RELATIVE_PATH = "skills/asd-ste100/SKILL.md"

logger = logging.getLogger(__name__)


def _build_backend(backend: str, timeout: int) -> AgentBackend:
    if backend == "opencode":
        return OpencodeBackend(timeout=timeout)
    return ClaudeBackend(timeout=timeout)


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
    """Review `target_path` in place using the asd-ste100 Strict-mode skill, via the
    `cfg.backend` CLI harness. Safe to call unconditionally: short-circuits when
    `cfg.enabled` is False, so callers don't need to guard the call themselves. Never
    raises -- any failure is logged as a warning and swallowed, so a prose-review
    failure never blocks the plan-stage caller that wrote `target_path`."""
    if not cfg.enabled:
        return

    try:
        repo_toplevel = git_ops.toplevel(Path(__file__).resolve().parent)
        skill_path = repo_toplevel / _SKILL_RELATIVE_PATH
        if not skill_path.exists():
            logger.warning("prose review skipped for %s: skill file not found at %s", target_path, skill_path)
            return

        relative_target = target_path.resolve().relative_to(repo_toplevel)
        prompt = _build_prompt(skill_path, relative_target)
        backend = _build_backend(cfg.backend, timeout)
        backend.invoke(prompt, cwd=repo_toplevel)
    except Exception as exc:
        logger.warning("prose review of %s failed: %s", target_path, exc)
