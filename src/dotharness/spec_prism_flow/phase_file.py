from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PHASE_FILE_NAME_PATTERN = re.compile(r"^(\d{2})-([a-z0-9-]+)-leaf\.md$")
_DEPENDS_ON_PHASE_NUMBER_PATTERN = re.compile(r"Phase\s+(\d+)", re.IGNORECASE)

_ACCEPTANCE_CRITERIA_HEADER = "Acceptance criteria"
_MANUAL_TEST_CHECKLIST_HEADER = "Manual test checklist"
_DEPENDS_ON_HEADER = "Depends on"

_SECTION_HEADERS = (
    "Scope",
    "Requirements",
    _ACCEPTANCE_CRITERIA_HEADER,
    _MANUAL_TEST_CHECKLIST_HEADER,
    _DEPENDS_ON_HEADER,
)


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

    @property
    def depends_on_phase_numbers(self) -> list[int]:
        return [int(n) for n in _DEPENDS_ON_PHASE_NUMBER_PATTERN.findall(self.depends_on)]


def phase_file_name(number: int, name: str) -> str:
    return f"{number:02d}-{name}-leaf.md"


def phase_file_stem(number: int, name: str) -> str:
    return phase_file_name(number, name).removesuffix(".md")


def phase_number_from_stem(stem: str) -> int:
    """The phase number from a `phase_file_stem`-shaped string (also a graph node
    name, per `decompose`'s own use of `phase_file_stem`): its numeric prefix before
    the first `-`."""
    return int(stem.split("-", 1)[0])


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
    return parse_phase_file_text(path.name, path.read_text())


def parse_phase_file_text(file_name: str, text: str) -> PhaseFile:
    name_match = PHASE_FILE_NAME_PATTERN.match(file_name)
    if not name_match:
        raise PhaseFileError(  # noqa: TRY003
            f"Phase file name '{file_name}' does not match the '<NN>-<slug>-leaf.md' pattern"
        )
    number = int(name_match.group(1))
    name = name_match.group(2)

    lines = text.splitlines()
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

    depends_items = _parse_bullets(bodies[_DEPENDS_ON_HEADER])
    if len(depends_items) != 1:
        raise PhaseFileError(  # noqa: TRY003
            f"'{_DEPENDS_ON_HEADER}' section must contain exactly one bullet item, got {len(depends_items)}"
        )
    depends_on = depends_items[0]

    return PhaseFile(
        number=number,
        name=name,
        scope=_parse_bullets(bodies["Scope"]),
        requirements=bodies["Requirements"].strip("\n"),
        acceptance_criteria=_parse_bullets(bodies[_ACCEPTANCE_CRITERIA_HEADER]),
        manual_test_checklist=_parse_bullets(bodies[_MANUAL_TEST_CHECKLIST_HEADER]),
        depends_on=depends_on,
    )


_FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
_SHALLOW_HEADING_PATTERN = re.compile(r"^#{1,2}(?=\s)")


def _demote_shallow_headings(markdown: str) -> str:
    """Rewrite every `#`/`##` heading (outside fenced code) as `###`, so an embedded mini-doc
    can't add `##` headers that break the phase file's five-header contract."""
    out = []
    in_fence = False
    for line in markdown.splitlines():
        if _FENCE_PATTERN.match(line):
            in_fence = not in_fence
        elif not in_fence:
            line = _SHALLOW_HEADING_PATTERN.sub("###", line)
        out.append(line)
    return "\n".join(out)


def render_phase_file(phase: PhaseFile) -> str:
    def render_bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items)

    sections = [
        ("Scope", render_bullets(phase.scope)),
        ("Requirements", _demote_shallow_headings(phase.requirements)),
        (_ACCEPTANCE_CRITERIA_HEADER, render_bullets(phase.acceptance_criteria)),
        (_MANUAL_TEST_CHECKLIST_HEADER, render_bullets(phase.manual_test_checklist)),
        (_DEPENDS_ON_HEADER, f"- {phase.depends_on}"),
    ]
    blocks = [f"## {header}\n{body}" for header, body in sections]
    return "\n\n".join(blocks) + "\n"
