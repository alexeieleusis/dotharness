from __future__ import annotations

import json
import time
from pathlib import Path

from spec_prism_flow import handoff
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.workspace import manifest_path

OVERVIEW_FILENAME = "00-overview.md"
OPEN_QUESTIONS_FILENAME = "OPEN_QUESTIONS.md"

_STAGE_NAME = "draft_overview"


class OverviewError(Exception):
    pass


def _load_manifest(workspace_dir: Path) -> dict:
    path = manifest_path(workspace_dir)
    if not path.exists():
        raise OverviewError(f"Manifest not found: {path}; run 'plan init' first")  # noqa: TRY003
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise OverviewError(f"Manifest is not valid JSON: {path}") from e  # noqa: TRY003
    if "brief" not in manifest:
        raise OverviewError(f"Manifest is missing required 'brief' key: {path}")  # noqa: TRY003
    return manifest


def build_overview_prompt(manifest: dict) -> str:
    lines = [
        f"# Draft {OVERVIEW_FILENAME}",
        "",
        f"You are drafting `{OVERVIEW_FILENAME}` for this project's phase corpus, modeled on "
        "Neighboku's `00-overview.md`. Read the brief below (and any code/conventions/links also "
        "listed) and produce a single markdown document with these sections, in order:",
        "",
        "1. Problem statement",
        "2. Goals",
        "3. Non-goals",
        "4. Glossary",
        "5. Key decisions already implied by the brief",
        "",
        "Every ambiguity you cannot resolve from the brief must become a concrete, answerable "
        f"question written to `{OPEN_QUESTIONS_FILENAME}` — never left implicit or silently assumed. "
        f"Omit `{OPEN_QUESTIONS_FILENAME}` entirely if the brief leaves nothing unresolved.",
        "",
        "## Inputs",
        "",
        f"- Brief: {manifest['brief']}",
        f"- Existing code: {manifest.get('code') or '(none provided)'}",
        f"- Conventions doc: {manifest.get('conventions') or '(none provided)'}",
    ]
    links = manifest.get("links") or []
    if links:
        lines.append("- Reference links:")
        lines.extend(f"  - {link}" for link in links)
    else:
        lines.append("- Reference links: (none provided)")
    return "\n".join(lines) + "\n"


def run_draft_overview(cfg: SpecPrismFlowConfig) -> tuple[Path, Path | None]:
    manifest = _load_manifest(cfg.plan.workspace_dir)
    prompt_text = build_overview_prompt(manifest)

    handoff_started_at = time.time()
    try:
        handoff.run_handoff(
            prompt_text,
            cfg.plan.workspace_dir,
            stage_name=_STAGE_NAME,
            output_filename=OVERVIEW_FILENAME,
        )
    except handoff.HandoffError as e:
        raise OverviewError(str(e)) from e

    overview_path = cfg.plan.workspace_dir / OVERVIEW_FILENAME
    if not overview_path.exists() or overview_path.stat().st_mtime < handoff_started_at:
        raise OverviewError(f"Expected output file was not written by this run: {overview_path}")  # noqa: TRY003

    open_questions_path = cfg.plan.workspace_dir / OPEN_QUESTIONS_FILENAME
    open_questions_written = open_questions_path.exists() and open_questions_path.stat().st_mtime >= handoff_started_at
    return overview_path, open_questions_path if open_questions_written else None
