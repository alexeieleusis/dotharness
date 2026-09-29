from __future__ import annotations

import logging
import re
from pathlib import Path

from spec_prism_flow import handoff, prose_review
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.overview_stage import OPEN_QUESTIONS_FILENAME, OVERVIEW_FILENAME
from spec_prism_flow.workspace import ManifestError, load_manifest

logger = logging.getLogger(__name__)

REQUIREMENTS_FILENAME = "requirements.md"
TEMPLATE_FILENAME = "requirements-doc-drafting-prompt.md"

_NUMBERED_HEADING_PATTERN = re.compile(r"^#{2,3}\s+(\d+(?:\.\d+)?)\.?\s+(.+)$")
SECTION_TOKEN_PATTERN = re.compile(r"§(\d+(?:\.\d+)?)(?![.\d])")

_STAGE_NAME = "draft_requirements"
_KNOWLEDGE_SUBDIR = "spec-prism-flow"


class RequirementsError(Exception):
    pass


def parse_numbered_headings(requirements_text: str) -> list[tuple[str, str, str]]:
    """(number, title, heading line) for each numbered `##`/`###` heading of requirements.md."""
    found = []
    for line in requirements_text.splitlines():
        match = _NUMBERED_HEADING_PATTERN.match(line.rstrip())
        if match:
            found.append((match.group(1), match.group(2).strip(), line.strip()))
    return found


def build_requirements_prompt(
    template_text: str,
    overview_text: str,
    open_questions_text: str | None,
    conventions_text: str | None,
) -> str:
    sections = [template_text, "\n\n## Approved overview\n\n" + overview_text]
    if open_questions_text:
        sections.append("\n\n## Open questions\n\n" + open_questions_text)
    if conventions_text:
        sections.append("\n\n## Architecture / convention constraints\n\n" + conventions_text)
    return "".join(sections)


def run_draft_requirements(cfg: SpecPrismFlowConfig) -> Path:
    try:
        manifest = load_manifest(cfg.plan.workspace_dir)
    except ManifestError as e:
        raise RequirementsError(str(e)) from e

    overview_path = cfg.plan.workspace_dir / OVERVIEW_FILENAME
    if not overview_path.exists():
        raise RequirementsError(f"Overview not found: {overview_path}; run 'plan draft-overview' first")  # noqa: TRY003
    overview_text = overview_path.read_text()

    open_questions_path = cfg.plan.workspace_dir / OPEN_QUESTIONS_FILENAME
    open_questions_text = open_questions_path.read_text() if open_questions_path.exists() else None

    conventions_path_str = manifest.get("conventions")
    conventions_text = None
    if conventions_path_str:
        conventions_path = Path(conventions_path_str)
        if not conventions_path.is_file():
            raise RequirementsError(f"Conventions file not found: {conventions_path}")  # noqa: TRY003
        conventions_text = conventions_path.read_text()

    template_path = cfg.harness.knowledge_dir / _KNOWLEDGE_SUBDIR / TEMPLATE_FILENAME
    if not template_path.exists():
        raise RequirementsError(f"Requirements drafting template not found: {template_path}")  # noqa: TRY003
    template_text = template_path.read_text()

    prompt_text = build_requirements_prompt(template_text, overview_text, open_questions_text, conventions_text)

    try:
        handoff.run_handoff(
            prompt_text,
            cfg.plan.workspace_dir,
            stage_name=_STAGE_NAME,
            output_filename=REQUIREMENTS_FILENAME,
        )
    except handoff.HandoffError as e:
        raise RequirementsError(str(e)) from e

    requirements_path = cfg.plan.workspace_dir / REQUIREMENTS_FILENAME
    prose_review.review_document(cfg.prose_review, requirements_path)

    # review_document is best-effort and never raises (a failure is logged and swallowed so a
    # prose review can't block the pipeline) — but a backend that *succeeds* can still blank or
    # mangle the document. Surface that silent-corruption case in the logs without changing the
    # never-block behavior: an empty/missing doc must not flow into the next stage unnoticed.
    if not requirements_path.is_file() or not requirements_path.read_text().strip():
        logger.warning(
            "prose review of %s left the requirements document empty or missing; the pipeline "
            "continues, but downstream stages may operate on a corrupted document",
            requirements_path,
        )

    return requirements_path
