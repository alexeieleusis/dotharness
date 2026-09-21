from __future__ import annotations

import re
from dataclasses import dataclass

from spec_prism_flow import graph as graph_module
from spec_prism_flow.config import SpecPrismFlowConfig
from spec_prism_flow.decompose import GRAPH_FILENAME
from spec_prism_flow.overview_stage import OPEN_QUESTIONS_FILENAME
from spec_prism_flow.phase_file import PhaseFile, PhaseFileError, parse_phase_file
from spec_prism_flow.requirements_stage import REQUIREMENTS_FILENAME

REVIEW_LOG_FILENAME = "review_log.md"

_LIST_ITEM_PATTERN = re.compile(r"^(?:-|\*|\d+\.)\s+(.*)$")
_RESOLVED_MARKER_PATTERN = re.compile(r"\*\*resolved", re.IGNORECASE)
_DEFERRED_MARKER_PATTERN = re.compile(r"\*\*deferred", re.IGNORECASE)
_REQUIREMENTS_HEADING_PATTERN = re.compile(r"^(#{2,3})\s+(\d+(?:\.\d+)?)\.?\s+(.+)$")


class ReviewError(Exception):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    violations: list[str]

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class ReviewReport:
    checks: list[CheckResult]

    @property
    def all_passed(self) -> bool:
        return all(check.passed for check in self.checks)


def _check_dependency_order(phase_files: list[PhaseFile]) -> list[str]:
    violations = []
    for pf in sorted(phase_files, key=lambda p: p.number):
        numbers = pf.depends_on_phase_numbers
        if pf.number == 1:
            if numbers:
                violations.append(
                    f"Phase 1 must depend on no phase ('None (first phase).'), but its "
                    f"'Depends on' text names phase(s) {numbers}: {pf.depends_on!r}"
                )
        else:
            expected = pf.number - 1
            if numbers != [expected]:
                found = numbers or "none"
                violations.append(
                    f"Phase {pf.number} must name exactly phase {expected} in its 'Depends on' "
                    f"text, but found {found}: {pf.depends_on!r}"
                )
    return violations


def _check_graph_agreement(graph: graph_module.Graph, phase_files: list[PhaseFile]) -> list[str]:
    return graph_module.validate_graph(graph, phase_files)


def _parse_requirements_sections(requirements_text: str) -> list[tuple[str, str]]:
    sections = []
    for line in requirements_text.splitlines():
        match = _REQUIREMENTS_HEADING_PATTERN.match(line.rstrip())
        if match:
            sections.append((match.group(2), match.group(3).strip()))
    return sections


def _check_section_coverage(requirements_text: str, phase_files: list[PhaseFile]) -> list[str]:
    combined_requirements = "\n".join(pf.requirements for pf in phase_files)
    violations = []
    for number, title in _parse_requirements_sections(requirements_text):
        if f"§{number}" not in combined_requirements:
            violations.append(f"requirements.md §{number} ({title}) is not referenced by any phase file")
    return violations


def _extract_list_items(text: str) -> list[str]:
    items: list[str] = []
    current: list[str] | None = None
    for line in text.splitlines():
        match = _LIST_ITEM_PATTERN.match(line)
        if match:
            if current is not None:
                items.append(" ".join(current))
            current = [match.group(1).strip()]
        elif current is not None and line.strip() and line[:1].isspace():
            current.append(line.strip())
        elif current is not None and not line.strip():
            items.append(" ".join(current))
            current = None
    if current is not None:
        items.append(" ".join(current))
    return items


def _check_stale_open_questions(open_questions_text: str | None) -> list[str]:
    if not open_questions_text or not open_questions_text.strip():
        return []
    violations = []
    for item in _extract_list_items(open_questions_text):
        if _RESOLVED_MARKER_PATTERN.search(item) or _DEFERRED_MARKER_PATTERN.search(item):
            continue
        violations.append(f"Stale open question with no recorded answer or explicit 'deferred' marker: {item}")
    return violations


def _render_report(report: ReviewReport) -> str:
    lines = ["# Spec Prism Flow — plan review report", ""]
    for check in report.checks:
        status = "PASS" if check.passed else "FAIL"
        lines.append(f"## {check.name}: {status}")
        if check.violations:
            lines.extend(f"- {v}" for v in check.violations)
        else:
            lines.append("- (no issues found)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def run_review(cfg: SpecPrismFlowConfig) -> ReviewReport:
    phase_dir = cfg.plan.phase_dir
    phase_paths = sorted(phase_dir.glob("*-leaf.md"))
    if not phase_paths:
        raise ReviewError(f"No phase files found in {phase_dir}; run 'plan draft-phases' first")  # noqa: TRY003
    try:
        phase_files = sorted((parse_phase_file(p) for p in phase_paths), key=lambda pf: pf.number)
    except PhaseFileError as e:
        raise ReviewError(str(e)) from e

    graph_path = phase_dir / GRAPH_FILENAME
    if not graph_path.exists():
        raise ReviewError(f"Graph file not found: {graph_path}; run 'plan decompose' first")  # noqa: TRY003
    try:
        loaded_graph = graph_module.load_graph(graph_path)
    except graph_module.GraphError as e:
        raise ReviewError(str(e)) from e

    requirements_path = cfg.plan.workspace_dir / REQUIREMENTS_FILENAME
    if not requirements_path.exists():
        raise ReviewError(  # noqa: TRY003
            f"Requirements doc not found: {requirements_path}; run 'plan draft-requirements' first"
        )
    requirements_text = requirements_path.read_text()

    open_questions_path = cfg.plan.workspace_dir / OPEN_QUESTIONS_FILENAME
    open_questions_text = open_questions_path.read_text() if open_questions_path.exists() else None

    checks = [
        CheckResult("Dependency order", _check_dependency_order(phase_files)),
        CheckResult("Graph agreement", _check_graph_agreement(loaded_graph, phase_files)),
        CheckResult("Section coverage", _check_section_coverage(requirements_text, phase_files)),
        CheckResult("Stale open questions", _check_stale_open_questions(open_questions_text)),
    ]
    report = ReviewReport(checks=checks)

    log_path = cfg.plan.workspace_dir / REVIEW_LOG_FILENAME
    log_path.write_text(_render_report(report))

    return report
