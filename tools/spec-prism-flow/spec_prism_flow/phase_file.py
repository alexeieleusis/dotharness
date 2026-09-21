from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PHASE_FILE_NAME_PATTERN = re.compile(r"^(\d{2})-([a-z0-9-]+)-leaf\.md$")

_ACCEPTANCE_CRITERIA_HEADER = "Acceptance criteria"
_MANUAL_TEST_CHECKLIST_HEADER = "Manual test checklist"

_SECTION_HEADERS = ("Scope", "Requirements", _ACCEPTANCE_CRITERIA_HEADER, _MANUAL_TEST_CHECKLIST_HEADER, "Depends on")


class PhaseFileError(ValueError):
    pass


@dataclass(frozen=True)
class PhaseFile:
    number: int
    name: str
    scope: list[str]
    requirements: str
    acceptance_criteria: list[str]
    manual_test_checklist: list[str]
    depends_on: str


def phase_file_name(number: int, name: str) -> str:
    return f"{number:02d}-{name}-leaf.md"


def _parse_bullets(body: str) -> list[str]:
    items = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if not line.startswith("- "):
            raise PhaseFileError(f"Expected a bullet list item starting with '- ', got: {line!r}")  # noqa: TRY003
        items.append(line[2:].strip())
    return items


def parse_phase_file(path: Path) -> PhaseFile:
    name_match = PHASE_FILE_NAME_PATTERN.match(path.name)
    if not name_match:
        raise PhaseFileError(  # noqa: TRY003
            f"Phase file name '{path.name}' does not match the '<NN>-<slug>-leaf.md' pattern"
        )
    number = int(name_match.group(1))
    name = name_match.group(2)

    lines = path.read_text().splitlines()
    header_indices = [i for i, line in enumerate(lines) if line.startswith("## ")]
    headers = [lines[i][3:].strip() for i in header_indices]

    if headers != list(_SECTION_HEADERS):
        raise PhaseFileError(  # noqa: TRY003
            f"Phase file must have exactly the five '##' headers {list(_SECTION_HEADERS)} in that order, got {headers}"
        )

    bodies = {}
    for idx, header in enumerate(_SECTION_HEADERS):
        start = header_indices[idx] + 1
        end = header_indices[idx + 1] if idx + 1 < len(header_indices) else len(lines)
        bodies[header] = "\n".join(lines[start:end]).strip("\n")

    depends_body = bodies["Depends on"].strip()
    depends_on = depends_body[2:].strip() if depends_body.startswith("- ") else depends_body

    return PhaseFile(
        number=number,
        name=name,
        scope=_parse_bullets(bodies["Scope"]),
        requirements=bodies["Requirements"].strip("\n"),
        acceptance_criteria=_parse_bullets(bodies[_ACCEPTANCE_CRITERIA_HEADER]),
        manual_test_checklist=_parse_bullets(bodies[_MANUAL_TEST_CHECKLIST_HEADER]),
        depends_on=depends_on,
    )


def render_phase_file(phase: PhaseFile) -> str:
    def render_bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    sections = [
        ("Scope", render_bullets(phase.scope)),
        ("Requirements", phase.requirements),
        (_ACCEPTANCE_CRITERIA_HEADER, render_bullets(phase.acceptance_criteria)),
        (_MANUAL_TEST_CHECKLIST_HEADER, render_bullets(phase.manual_test_checklist)),
        ("Depends on", f"- {phase.depends_on}"),
    ]
    blocks = [f"## {header}\n{body}" for header, body in sections]
    return "\n\n".join(blocks) + "\n"
