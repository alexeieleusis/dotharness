from __future__ import annotations

import json
from pathlib import Path

from spec_prism_flow import handoff
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.overview_stage import OPEN_QUESTIONS_FILENAME, OVERVIEW_FILENAME
from spec_prism_flow.workspace import manifest_path

REQUIREMENTS_FILENAME = "requirements.md"
TEMPLATE_FILENAME = "requirements-doc-drafting-prompt.md"

_STAGE_NAME = "draft_requirements"
_KNOWLEDGE_SUBDIR = "spec-prism-flow"


class RequirementsError(Exception):
    pass


def _load_manifest(workspace_dir: Path) -> dict:
    path = manifest_path(workspace_dir)
    if not path.exists():
        raise RequirementsError(f"Manifest not found: {path}; run 'plan init' first")  # noqa: TRY003
    return json.loads(path.read_text())


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
    manifest = _load_manifest(cfg.plan.workspace_dir)

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
    if not requirements_path.exists():
        raise RequirementsError(f"Expected output file not found: {requirements_path}")  # noqa: TRY003

    return requirements_path
