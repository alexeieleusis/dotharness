from __future__ import annotations

import logging
from pathlib import Path

from spec_prism_flow.build import git_ops
from spec_prism_flow.build.agent_runner import make_backend
from spec_prism_flow.config import ProseReviewConfig

# Not either backend's own 1800-second full-build-cycle default: a prose review is a
# single-file copy-edit, not a full agent build cycle.
DEFAULT_PROSE_REVIEW_TIMEOUT_SECONDS = 900  # 15 minutes

_SKILL_RELATIVE_PATH = "skills/asd-ste100/SKILL.md"

logger = logging.getLogger(__name__)


def _skill_base() -> Path:
    """spec-prism-flow's own install root, used to locate the asd-ste100 skill.

    The skill ships in spec-prism-flow's own repo tree (the monorepo root's `skills/`
    dir) -- a location entirely separate from whatever target project this tool is
    pointed at. It's resolved against where this package itself is installed, so it
    never depends on the target's repo toplevel (which is where the target document
    actually lives). Plain path arithmetic, not git, so it can't raise the way
    `git_ops.toplevel` does in a non-git packaged install (e.g. `site-packages`).
    `prose_review.py` lives at `<root>/tools/spec-prism-flow/spec_prism_flow/`, so the
    monorepo root -- and with it the `skills/` dir -- is `parents[3]`."""
    return Path(__file__).resolve().parents[3]


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
        # The target document lives in the target project's own repo -- usually a
        # *different* repo from where spec-prism-flow itself is installed. Resolve the
        # document's repo-relative path and the backend's working directory against
        # that repo's toplevel, never against this tool's own tree.
        resolved_target = target_path.resolve()
        target_toplevel = git_ops.toplevel(resolved_target.parent)
        relative_target = resolved_target.relative_to(target_toplevel)

        skill_path = _skill_base() / _SKILL_RELATIVE_PATH
        if not skill_path.exists():
            logger.warning("prose review skipped for %s: skill file not found at %s", target_path, skill_path)
            return

        prompt = _build_prompt(skill_path, relative_target)
        backend = make_backend(cfg.backend, timeout)
        backend.invoke(prompt, cwd=target_toplevel)
    except Exception as exc:
        logger.warning("prose review of %s failed: %s", target_path, exc)
