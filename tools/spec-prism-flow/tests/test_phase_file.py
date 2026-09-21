from pathlib import Path

import pytest

from spec_prism_flow.phase_file import (
    PHASE_FILE_NAME_PATTERN,
    PhaseFile,
    PhaseFileError,
    parse_phase_file,
    phase_file_name,
    render_phase_file,
)

WELL_FORMED_TEXT = """## Scope
- spec_prism_flow/config.py
- tests/test_config.py

## Requirements
Some requirements text.

It may span multiple paragraphs.

## Acceptance criteria
- load_config raises ConfigError for bad input.
- Tests pass.

## Manual test checklist
- Run the tests manually.

## Depends on
- None (first phase).
"""


def test_phase_file_name():
    assert phase_file_name(1, "config-and-phase-file") == "01-config-and-phase-file-leaf.md"


def test_phase_file_name_pattern_matches():
    assert PHASE_FILE_NAME_PATTERN.match("01-config-and-phase-file-leaf.md")


def test_phase_file_name_pattern_rejects_missing_leaf_suffix():
    assert PHASE_FILE_NAME_PATTERN.match("01-config-and-phase-file.md") is None


def test_parse_phase_file(tmp_path):
    p = tmp_path / "01-config-and-phase-file-leaf.md"
    p.write_text(WELL_FORMED_TEXT)

    phase = parse_phase_file(p)

    assert phase.number == 1
    assert phase.name == "config-and-phase-file"
    assert phase.scope == ["spec_prism_flow/config.py", "tests/test_config.py"]
    assert phase.requirements == "Some requirements text.\n\nIt may span multiple paragraphs."
    assert phase.acceptance_criteria == [
        "load_config raises ConfigError for bad input.",
        "Tests pass.",
    ]
    assert phase.manual_test_checklist == ["Run the tests manually."]
    assert phase.depends_on == "None (first phase)."


def test_round_trips_byte_for_byte(tmp_path):
    p = tmp_path / "01-config-and-phase-file-leaf.md"
    p.write_text(WELL_FORMED_TEXT)

    phase = parse_phase_file(p)

    assert render_phase_file(phase) == WELL_FORMED_TEXT


def test_render_phase_file_directly():
    phase = PhaseFile(
        number=9,
        name="sample",
        scope=["a.py"],
        requirements="Req body.",
        acceptance_criteria=["Crit one."],
        manual_test_checklist=["Check one."],
        depends_on="Phase 8 merged.",
    )

    rendered = render_phase_file(phase)

    assert rendered == (
        "## Scope\n- a.py\n\n"
        "## Requirements\nReq body.\n\n"
        "## Acceptance criteria\n- Crit one.\n\n"
        "## Manual test checklist\n- Check one.\n\n"
        "## Depends on\n- Phase 8 merged.\n"
    )


def test_missing_filename_pattern_raises(tmp_path):
    p = tmp_path / "not-a-phase-file.md"
    p.write_text(WELL_FORMED_TEXT)

    with pytest.raises(PhaseFileError, match="leaf"):
        parse_phase_file(p)


def test_missing_header_raises(tmp_path):
    text = WELL_FORMED_TEXT.replace("## Depends on\n- None (first phase).\n", "")
    p = tmp_path / "01-sample-leaf.md"
    p.write_text(text)

    with pytest.raises(PhaseFileError, match="Depends on"):
        parse_phase_file(p)


def test_reordered_headers_raises(tmp_path):
    text = """## Requirements
Some requirements text.

## Scope
- a.py

## Acceptance criteria
- Crit one.

## Manual test checklist
- Check one.

## Depends on
- None (first phase).
"""
    p = tmp_path / "01-sample-leaf.md"
    p.write_text(text)

    with pytest.raises(PhaseFileError):
        parse_phase_file(p)


def test_extra_unexpected_header_raises(tmp_path):
    text = WELL_FORMED_TEXT + "\n## Extra section\n- something\n"
    p = tmp_path / "01-sample-leaf.md"
    p.write_text(text)

    with pytest.raises(PhaseFileError):
        parse_phase_file(p)


def test_misspelled_header_raises(tmp_path):
    text = WELL_FORMED_TEXT.replace("## Acceptance criteria", "## Aceptance criteria")
    p = tmp_path / "01-sample-leaf.md"
    p.write_text(text)

    with pytest.raises(PhaseFileError):
        parse_phase_file(p)


def test_non_bullet_line_in_scope_raises(tmp_path):
    text = WELL_FORMED_TEXT.replace("- spec_prism_flow/config.py", "spec_prism_flow/config.py")
    p = tmp_path / "01-sample-leaf.md"
    p.write_text(text)

    with pytest.raises(PhaseFileError, match="bullet list item"):
        parse_phase_file(p)


def test_real_phase_file_parses(tmp_path):
    real_path = Path(__file__).parent.parent / "docs" / "phases" / "01-config-and-phase-file-leaf.md"

    phase = parse_phase_file(real_path)

    assert phase.number == 1
    assert phase.name == "config-and-phase-file"
    assert phase.depends_on == "None (first phase)."
