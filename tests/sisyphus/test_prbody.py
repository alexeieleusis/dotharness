from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from dotharness.sisyphus import prbody
from dotharness.sisyphus.pick import mentions_issue
from dotharness.sisyphus.prbody import (
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    PLAN_NOT_MATCHED_NOTE,
    _refactor_handoff_lines,
    _refactor_record_lines,
    clean_title,
    commit_log_args,
    compose_body,
    derive_title,
    extract_decisions,
    extract_deviations,
    extract_summary,
    fallback_title,
    format_decisions,
    format_refactor_section,
    leaf_stems,
    list_phases,
    locate_phase_dir,
    locate_plan_dir,
    parse_commits,
    plan_dir_matches_brief,
    read_requirements,
    redact_paths,
    sanitise,
    select_handoffs,
    work_item_header,
)
from tests.sisyphus.conftest import forbidden_imports

STEMS = ["01-alpha-leaf", "02-beta-leaf"]


def test_module_imports_neither_harness_nor_spec_prism() -> None:
    assert not forbidden_imports(Path(prbody.__file__).read_text())
    assert PLAN_NOT_MATCHED_NOTE.startswith("Note:")


def test_locate_dirs(tmp_path: Path) -> None:
    spf = tmp_path / "spf.toml"
    spf.write_text('[plan]\nworkspace_dir = "ws"\nphase_dir = "/abs/phases"\n')
    assert locate_plan_dir(spf, tmp_path / "repo") == tmp_path / "repo" / "ws"
    assert locate_phase_dir(spf, tmp_path) == Path("/abs/phases")
    assert locate_plan_dir(tmp_path / "missing.toml", tmp_path) is None
    spf.write_text("not [ toml")
    assert locate_plan_dir(spf, tmp_path) is None
    spf.write_text("[agent]\n")
    assert locate_plan_dir(spf, tmp_path) is None


def test_ownership(tmp_path: Path) -> None:
    brief = tmp_path / "b.md"
    brief.write_text("x")
    plan = tmp_path / "plan"
    plan.mkdir()
    assert not plan_dir_matches_brief(plan, brief)
    (plan / "init_manifest.json").write_text("{bad")
    assert not plan_dir_matches_brief(plan, brief)
    (plan / "init_manifest.json").write_text("{}")
    assert not plan_dir_matches_brief(plan, brief)
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "other.md")}))
    assert not plan_dir_matches_brief(plan, brief)
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "sub" / ".." / "b.md")}))
    assert plan_dir_matches_brief(plan, brief)


def test_leaf_stems(tmp_path: Path) -> None:
    (tmp_path / "02-b-leaf.md").write_text("")
    (tmp_path / "01-a-leaf.md").write_text("")
    (tmp_path / "notes.md").write_text("")
    assert leaf_stems(tmp_path) == ["01-a-leaf", "02-b-leaf"]
    (tmp_path / "graph.json").write_text("{bad")
    assert leaf_stems(tmp_path) == ["01-a-leaf", "02-b-leaf"]
    (tmp_path / "graph.json").write_text(json.dumps({"nodes": ["02-b-leaf", "01-a-leaf"]}))
    assert leaf_stems(tmp_path) == ["02-b-leaf", "01-a-leaf"]


def test_select_handoffs(tmp_path: Path) -> None:
    for n in ("01-alpha-leaf.md", "01-alpha-leaf-followup.md", "02-02-beta-leaf.md", "1-alpha-leaf.md", "01-other.md"):
        (tmp_path / n).write_text("")
    got = select_handoffs(tmp_path, STEMS)
    assert [p.name for p in got["01-alpha-leaf"]] == ["01-alpha-leaf-followup.md", "01-alpha-leaf.md"]
    assert [p.name for p in got["02-beta-leaf"]] == ["02-02-beta-leaf.md"]


def test_select_handoffs_ignores_refactor_handoffs(tmp_path: Path) -> None:
    for n in (
        "refactor-generic.md",
        "refactor-fastapi.md",
        "refactor-react.md",
        "refactor-01-alpha-leaf.md",
        "refactor-02-beta-leaf.md",
        "refactor-alpha-leaf.md",
        "refactor-beta-leaf.md",
    ):
        (tmp_path / n).write_text("")
    assert select_handoffs(tmp_path, STEMS) == {"01-alpha-leaf": [], "02-beta-leaf": []}
    (tmp_path / "01-alpha-leaf.md").write_text("")
    got = select_handoffs(tmp_path, STEMS)
    assert [p.name for p in got["01-alpha-leaf"]] == ["01-alpha-leaf.md"]
    assert got["02-beta-leaf"] == []


def test_summary(tmp_path: Path) -> None:
    (tmp_path / "requirements.md").write_text(
        "# T\n## 1. Purpose\nP\n## 2. Problem\nQ\nmore\n## 3. Goals\nG\n## 5. Glossary\nX\n"
    )
    out = extract_summary(tmp_path)
    assert "P" in out and "Q\nmore" in out and "G" in out and "Glossary" not in out
    assert "## 4." not in out


def test_summary_degradation(tmp_path: Path) -> None:
    assert extract_summary(tmp_path).startswith("Note:")
    (tmp_path / "requirements.md").write_text("## 5. Other\nx\n")
    assert extract_summary(tmp_path).startswith("Note:")
    assert "\n" not in extract_summary(tmp_path)


def _phase_dir(tmp_path: Path) -> Path:
    (tmp_path / "graph.json").write_text(json.dumps({"nodes": STEMS}))
    return tmp_path


def test_commit_log_args_and_parse() -> None:
    assert commit_log_args("main")[-1] == "origin/main..HEAD"
    assert parse_commits("abc\tPhase 01: x\nbad line\n\n") == [("abc", "Phase 01: x")]


def test_list_phases(tmp_path: Path) -> None:
    log = "aaa\tPhase 01: whatever\nbbb\tsomething\nccc\tPhase 01: beta-leaf done\n"
    out = list_phases(_phase_dir(tmp_path), log).splitlines()
    assert out[0] == "- 01-alpha-leaf: aaa Phase 01: whatever"
    # stem match wins over the number match
    assert out[1] == "- 02-beta-leaf: ccc Phase 01: beta-leaf done"
    assert out[-2:] == ["Commits not matched to a phase:", "- bbb something"]


def test_list_phases_leaf_without_commit(tmp_path: Path) -> None:
    out = list_phases(_phase_dir(tmp_path), "")
    assert out == "- 01-alpha-leaf\n- 02-beta-leaf"


def test_list_phases_degradation(tmp_path: Path) -> None:
    assert list_phases(_phase_dir(tmp_path), None).startswith("Note:")
    assert list_phases(tmp_path / "nope", "").startswith("Note:")


def _handoff(deviation: str) -> str:
    return f"### Handoff\n- Done: x\n- Deviations from the plan: {deviation}\n- Context: y\n"


def test_deviations(tmp_path: Path) -> None:
    (tmp_path / "01-alpha-leaf.md").write_text(
        "- Done: x\n- Deviations from the plan: Used A\n  instead of B.\n\nmore\n- Context: y\n"
    )
    (tmp_path / "02-beta-leaf.md").write_text(_handoff("None."))
    out = extract_deviations(tmp_path, STEMS)
    assert out == "- 01-alpha-leaf: Used A\n  instead of B.\n\nmore"


def test_deviations_all_none(tmp_path: Path) -> None:
    (tmp_path / "01-alpha-leaf.md").write_text(_handoff("none"))
    (tmp_path / "02-beta-leaf.md").write_text(_handoff("None!"))
    assert extract_deviations(tmp_path, STEMS) == "None reported."


def test_deviations_notes(tmp_path: Path) -> None:
    (tmp_path / "01-alpha-leaf.md").write_text("- Done: x\n")
    out = extract_deviations(tmp_path, STEMS)
    assert "no deviations bullet found in handoff 01-alpha-leaf.md" in out
    assert "no handoff found for 02-beta-leaf" in out
    assert "None reported" not in out


def test_deviations_missing_dir(tmp_path: Path) -> None:
    out = extract_deviations(tmp_path / "handoffs", STEMS)
    assert out.startswith("Note:") and "\n" not in out


def test_deviation_with_none_and_a_missing_handoff_is_not_none_reported(tmp_path: Path) -> None:
    (tmp_path / "01-alpha-leaf.md").write_text(_handoff("None"))
    out = extract_deviations(tmp_path, STEMS)
    assert out == "Note: no handoff found for 02-beta-leaf."


def test_deviations_none_with_explanation(tmp_path: Path) -> None:
    (tmp_path / "01-alpha-leaf.md").write_text(_handoff("None. Defaults D-1 applied."))
    (tmp_path / "02-beta-leaf.md").write_text(_handoff("None (used defaults)"))
    assert extract_deviations(tmp_path, STEMS) == "None reported."
    (tmp_path / "02-beta-leaf.md").write_text(_handoff("None, see notes"))
    assert extract_deviations(tmp_path, STEMS) == "None reported."


def test_deviations_none_lookalikes_are_deviations(tmp_path: Path) -> None:
    (tmp_path / "01-alpha-leaf.md").write_text(_handoff("None of the checks ran"))
    (tmp_path / "02-beta-leaf.md").write_text(_handoff("Nonexistent files were touched"))
    out = extract_deviations(tmp_path, STEMS).splitlines()
    assert out == ["- 01-alpha-leaf: None of the checks ran", "- 02-beta-leaf: Nonexistent files were touched"]


def test_deviations_all_missing_is_one_line(tmp_path: Path) -> None:
    out = extract_deviations(tmp_path, STEMS)
    assert out == "Note: no handoffs found for any of the 2 leaves; deviations could not be checked."


BRIEF = "# B\n\n## Open decisions log\n\n- D-1: Who owns it? Status: resolved - the item.\n- D-2: Resolved: use GitHub.\n- D-3: open?\n"


def test_format_decisions() -> None:
    out = format_decisions(BRIEF).splitlines()
    assert len(out) == 2
    assert out[0].startswith("- 1. ") and "Who owns it?" in out[0] and "the item" in out[0]
    assert "use GitHub" in out[1]


def test_format_decisions_empty() -> None:
    assert format_decisions("# B\n\nnothing\n") == ""


def test_extract_decisions(tmp_path: Path) -> None:
    brief = tmp_path / "b.md"
    brief.write_text(BRIEF)
    assert "Who owns it?" in extract_decisions(brief)
    assert extract_decisions(tmp_path / "missing.md").startswith("Note:")


# ------------------------------------------------------ sanitise and compose


def test_sanitise_blocks_mentions_and_issue_refs() -> None:
    text = "#5 start\nsee (#6), x:#7. @alice and @bob-x\n```\n#8 @carol\n```\nowner/repo#9 a@b"
    out = sanitise(text)
    for n in (5, 6, 7, 8, 9):
        assert not mentions_issue(out, n)
    assert "@alice" not in out and "@carol" not in out and "@bob" not in out
    assert "a@\u200bb" in out
    assert "</details>" not in sanitise("x </DETAILS> y </details>")


def test_redact_paths_each_form_longest_first(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ws = home / "ws"
    plan = ws / "plan-1"
    text = f"{plan}/requirements.md {ws}/handoffs {home}/x $HOME/y ${{HOME}}/z ~/ok"
    out = redact_paths(text, ws, plan, home)
    assert out == "<plan-dir>/requirements.md <workspace>/handoffs ~/x ~/y ~/z ~/ok"


def _compose(
    tmp_path: Path,
    *,
    summary: str = "",
    phases: str = "",
    decisions: str = "",
    deviations: str = "",
    requirements: str | None = None,
) -> str:
    ws = tmp_path / "ws"
    return compose_body(
        "o/r#109",
        summary=summary,
        phases=phases,
        decisions=decisions,
        deviations=deviations,
        requirements=requirements,
        workspace=ws,
        plan_dir=ws / "plan",
        home=tmp_path,
    )


def test_compose_order_and_header(tmp_path: Path) -> None:
    body = _compose(
        tmp_path,
        summary="S #109 @bob",
        phases="P",
        decisions="D",
        deviations="V",
        requirements=f"# Req {tmp_path}/ws/x\n</details>\n#12",
    )
    assert body.startswith("Closes o/r#109\n\n## Summary")
    idx = [body.index(m) for m in ("## Summary", "## Phases", "## Decisions", "## Deviations", "<details>")]
    assert idx == sorted(idx)
    assert body.count("</details>") == 1 and body.endswith("</details>")
    assert str(tmp_path) not in body
    assert "<summary>Full requirements</summary>\n\n# Req" in body
    assert not mentions_issue(body, 109) and not mentions_issue(body, 12)
    assert (
        compose_body("o/r#109", workspace=tmp_path)
        == "Closes o/r#109\n\nNote: `requirements.md` is missing or unreadable; full requirements omitted."
    )


def test_compose_oversize_drops_details_first(tmp_path: Path) -> None:
    body = _compose(tmp_path, summary="short", requirements="x" * (MAX_BODY_CHARS + 10))
    assert "<details>" not in body and "short" in body
    assert "<plan-dir>/requirements.md" in body and len(body) <= MAX_BODY_CHARS


def test_compose_oversize_truncates_remaining(tmp_path: Path) -> None:
    body = _compose(tmp_path, summary="```\n" + "y" * (MAX_BODY_CHARS * 2), phases="late", requirements="r")
    assert len(body) <= MAX_BODY_CHARS
    assert body.startswith("Closes o/r#109")
    assert "late" not in body and "truncated" in body and "<plan-dir>/requirements.md" in body
    assert body.count("```") % 2 == 0


def test_read_requirements_degrades(tmp_path: Path) -> None:
    assert read_requirements(None) is None
    assert read_requirements(tmp_path) is None
    (tmp_path / "requirements.md").write_bytes(b"\xff\xfe")
    assert read_requirements(tmp_path) is None
    (tmp_path / "requirements.md").write_text("ok")
    assert read_requirements(tmp_path) == "ok"


async def _build(tmp_path: Path, plan: Path, git_out: str | None = "abc\tPhase 01: alpha\n") -> str:
    async def run_git(_args: object) -> str | None:
        return git_out

    return await prbody.build_body(
        "w",
        brief_path=tmp_path / "b.md",
        plan_dir=plan,
        phase_dir=_phase_dir(tmp_path / "phases"),
        base_branch="main",
        workspace=tmp_path,
        run_git=run_git,
    )


async def test_build_body_full_and_degraded(tmp_path: Path) -> None:
    (tmp_path / "phases").mkdir()
    (tmp_path / "b.md").write_text("x")
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "requirements.md").write_text("## 1. Goal\n\nShip it.\n")
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "b.md")}))
    body = await _build(tmp_path, plan)
    assert "Ship it." in body
    assert "01-alpha-leaf: abc Phase 01: alpha" in body
    assert "Phases unavailable: git log failed" in await _build(tmp_path, plan, None)
    (plan / "init_manifest.json").unlink()
    assert PLAN_NOT_MATCHED_NOTE in await _build(tmp_path, plan)


async def _build_refactor(tmp_path: Path, **kw: Any) -> str:
    plan = tmp_path / "plan"
    plan.mkdir(exist_ok=True)
    (tmp_path / "phases").mkdir(exist_ok=True)
    (tmp_path / "b.md").write_text("x")
    (plan / "requirements.md").write_text("## 1. Goal\n\nShip it.\n")
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "b.md")}))

    async def run_git(_args: object) -> str | None:
        return "abc\tPhase 01: alpha\n"

    return await prbody.build_body(
        "w",
        brief_path=tmp_path / "b.md",
        plan_dir=plan,
        phase_dir=_phase_dir(tmp_path / "phases"),
        base_branch="main",
        workspace=tmp_path,
        run_git=run_git,
        **kw,
    )


def _spy_refactor(monkeypatch: Any) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"extract": [], "format": []}

    def extract(paths: Sequence[Path]) -> list[Any]:
        calls["extract"].append(list(paths))
        return [prbody.RefactorHandoff(p.name[len("refactor-") : -3], None, None, ()) for p in paths]

    def fmt(records: Sequence[Any], results: Any) -> str:
        calls["format"].append((list(records), results))
        return "X"

    monkeypatch.setattr(prbody, "extract_refactor_handoffs", extract)
    monkeypatch.setattr(prbody, "format_refactor_section", fmt)
    return calls


def _rec(name: Any) -> dict[str, Any]:
    return {"set": name, "files": [], "evidence": [], "fallback": False}


async def test_build_body_refactor_none_or_empty(tmp_path: Path, monkeypatch: Any) -> None:
    base = await _build_refactor(tmp_path)
    calls = _spy_refactor(monkeypatch)
    assert "## Refactor" not in base
    assert await _build_refactor(tmp_path, refactor_records=[], refactor_handoff_dir=tmp_path) == base
    assert await _build_refactor(tmp_path, refactor_records=["s", None, {"x": 1}, {"set": 3}]) == base
    assert calls == {"extract": [], "format": []}


async def test_build_body_refactor_paths_and_formatter(tmp_path: Path, monkeypatch: Any) -> None:
    calls = _spy_refactor(monkeypatch)
    hd = tmp_path / "h"
    hd.mkdir()
    (hd / "refactor-a.md").write_text("x")
    recs = [_rec("a"), _rec("b")]
    body = await _build_refactor(tmp_path, refactor_records=recs, refactor_handoff_dir=hd)
    assert calls["extract"] == [[hd / "refactor-a.md"]]
    ((got_recs, got_results),) = calls["format"]
    assert got_recs == recs
    assert set(got_results) == {"a"}
    assert body.index("## Deviations") < body.index("## Refactor\n\nX")


async def test_build_body_refactor_defensive_records(tmp_path: Path, monkeypatch: Any) -> None:
    calls = _spy_refactor(monkeypatch)
    good = _rec("g")
    await _build_refactor(tmp_path, refactor_records=["s", None, {"files": []}, {"set": 3}, good])
    assert calls["format"][0][0] == [good]


async def test_build_body_refactor_unsafe_set_names(tmp_path: Path, monkeypatch: Any) -> None:
    calls = _spy_refactor(monkeypatch)
    hd = tmp_path / "h"
    hd.mkdir()
    # Real files for every candidate path the guard must reject, plus a positive control.
    for n in ("..", ".", "", "a\\b"):
        (hd / f"refactor-{n}.md").write_text("x")
    (hd / "refactor-..").mkdir()
    (hd / "refactor-..").joinpath("x.md").write_text("x")  # candidate for "../x"
    (hd / "refactor-a").mkdir()
    (hd / "refactor-a" / "b.md").write_text("x")  # candidate for "a/b"
    (hd / "refactor-ok.md").write_text("x")
    names = ["../x", "a/b", "a\\b", "..", ".", "", "a\0b", "ok"]
    recs = [_rec(n) for n in names]
    await _build_refactor(tmp_path, refactor_records=recs, refactor_handoff_dir=hd)
    assert calls["extract"] == [[hd / "refactor-ok.md"]]
    assert calls["format"][0][0] == recs


async def test_build_body_refactor_no_dir_and_directory(tmp_path: Path, monkeypatch: Any) -> None:
    calls = _spy_refactor(monkeypatch)
    recs = [_rec("a")]
    await _build_refactor(tmp_path, refactor_records=recs)
    hd = tmp_path / "h"
    (hd / "refactor-a.md").mkdir(parents=True)
    await _build_refactor(tmp_path, refactor_records=recs, refactor_handoff_dir=hd)
    assert calls["extract"] == [[], []]
    assert [c[0] for c in calls["format"]] == [recs, recs]


async def test_build_body_refactor_integration_fallback(tmp_path: Path) -> None:
    hd = tmp_path / "h"
    hd.mkdir()
    (hd / "refactor-generic.md").write_text("- Deviations from the plan: None\n")
    rec = {"set": "generic", "files": ["a.py"], "evidence": ["no manifest found"], "fallback": True}
    body = await _build_refactor(tmp_path, refactor_records=[rec], refactor_handoff_dir=hd)
    assert "## Refactor" in body and "### generic" in body
    assert "- Fallback: yes" in body and "no manifest found" in body


async def test_build_body_refactor_is_keyword_only(tmp_path: Path) -> None:
    async def run_git(_args: object) -> str | None:
        return None

    build: Any = prbody.build_body
    with pytest.raises(TypeError):
        await build("w", tmp_path, tmp_path, tmp_path, "main", tmp_path, run_git, [], None)


async def test_build_body_reads_handoffs_from_plan_dir(tmp_path: Path) -> None:
    (tmp_path / "phases").mkdir()
    (tmp_path / "b.md").write_text("x")
    plan = tmp_path / "plan"
    (plan / "handoffs").mkdir(parents=True)
    (plan / "requirements.md").write_text("## 1. Goal\n\nShip it.\n")
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "b.md")}))
    (plan / "handoffs" / "01-alpha-leaf.md").write_text("- Deviations from the plan: Used A instead of B.\n")
    assert "Used A instead of B." in await _build(tmp_path, plan)


async def test_build_body_deviation_notes(tmp_path: Path) -> None:
    (tmp_path / "phases").mkdir()
    (tmp_path / "b.md").write_text("x")
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "requirements.md").write_text("## 1. Goal\n\nShip it.\n")
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "b.md")}))
    (plan / "handoffs").mkdir()
    body = await _build(tmp_path, plan)
    assert "Note: no handoffs found for any of the 2 leaves; deviations could not be checked." in body
    assert "no handoff found for" not in body
    (plan / "handoffs" / "01-alpha-leaf.md").write_text(_handoff("None."))
    body = await _build(tmp_path, plan)
    assert "Note: no handoff found for 02-beta-leaf." in body
    assert "for any of" not in body


# ---------------------------------------------------------------- title


def _owned_plan(tmp_path: Path, requirements: str | None, brief_text: str = "") -> tuple[Path, Path]:
    brief = tmp_path / "brief.md"
    brief.write_text(brief_text)
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(brief)}))
    if requirements is not None:
        (plan / "requirements.md").write_text(requirements)
    return brief, plan


def test_title_requirements_h1_wins_over_brief(tmp_path: Path) -> None:
    brief, plan = _owned_plan(tmp_path, "# Requirements: from reqs (#1)\n", "# Brief: from brief (#2)\n")
    assert derive_title(brief, plan, tmp_path) == "from reqs (#1)"


def test_title_brief_when_plan_not_owned(tmp_path: Path) -> None:
    brief, plan = _owned_plan(tmp_path, "# Requirements: from reqs\n", "# Brief: from brief (#2)\n")
    (plan / "init_manifest.json").write_text(json.dumps({"brief": str(tmp_path / "other.md")}))
    assert derive_title(brief, plan, tmp_path) == "from brief (#2)"
    assert derive_title(brief, None, tmp_path) == "from brief (#2)"


def test_title_brief_when_requirements_missing_or_headless(tmp_path: Path) -> None:
    brief, plan = _owned_plan(tmp_path, None, "# Brief: from brief\n")
    assert derive_title(brief, plan, tmp_path) == "from brief"
    (plan / "requirements.md").write_text("no heading\n## Sub\n")
    assert derive_title(brief, plan, tmp_path) == "from brief"
    (plan / "requirements.md").write_text("# Requirements:   \n")
    assert derive_title(brief, plan, tmp_path) == "from brief"


def test_title_none_without_h1_or_with_empty_h1(tmp_path: Path) -> None:
    brief, plan = _owned_plan(tmp_path, "## only sub\n", "#\n# Brief:\n## x\n")
    assert derive_title(brief, plan, tmp_path) is None
    assert derive_title(tmp_path / "missing.md", None, tmp_path) is None


def test_title_prefix_stripping(tmp_path: Path) -> None:
    for heading, expected in [
        ("# Requirements: auto-approve CI check (dotharness #118)", "auto-approve CI check (dotharness #118)"),
        ("# Brief: Sisyphus is done (#124)", "Sisyphus is done (#124)"),
        ("# requirements:\tlower case", "lower case"),
        ("# BRIEF:x", "x"),
        ("# Plain heading", "Plain heading"),
        ("# Requirements: Brief: twice", "Brief: twice"),
    ]:
        brief = tmp_path / "b.md"
        brief.write_text(heading + "\n")
        assert derive_title(brief, None, tmp_path) == expected


def test_title_h1_outside_fences_only_first_counts(tmp_path: Path) -> None:
    brief = tmp_path / "b.md"
    brief.write_text("## sub\n```\n# in fence\n```\n~~~\n# tilde fence\n~~~\n# Real one\n# Second\n")
    assert derive_title(brief, None, tmp_path) == "Real one"


def test_title_mentions_neutralised_issue_refs_untouched(tmp_path: Path) -> None:
    brief = tmp_path / "b.md"
    brief.write_text("# Brief: ping @alice about #12 and a@b\n")
    title = derive_title(brief, None, tmp_path)
    assert title == "ping @​alice about #12 and a@​b"


def test_title_single_line() -> None:
    assert clean_title("a\r\nb\t\tc \n d", Path("/ws"), None, Path("/nohome")) == "a b c d"


def test_title_cap_with_ellipsis() -> None:
    ws = Path("/ws")
    for n, expected_len in [(255, 255), (256, 256), (257, 256), (1000, 256)]:
        out = clean_title("x" * n, ws, None, Path("/nohome"))
        assert len(out) == expected_len
    assert clean_title("x" * 256, ws, None, Path("/nohome")) == "x" * 256
    long = clean_title("x" * 257, ws, None, Path("/nohome"))
    assert long == "x" * 253 + "..."
    assert MAX_TITLE_CHARS == 256


def test_title_paths_redacted(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    plan = ws / "plan"
    brief = tmp_path / "b.md"
    brief.write_text(f"# Brief: fix {plan} and {ws} in\n")
    assert derive_title(brief, None, ws, tmp_path / "nohome") == "fix <workspace>/plan and <workspace> in"
    assert derive_title(brief, plan, ws, tmp_path / "nohome") == "fix <plan-dir> and <workspace> in"


def test_fallback_title_and_cap() -> None:
    assert fallback_title("item 7") == "Sisyphus: item 7"
    out = fallback_title("y" * 400)
    assert len(out) == 256
    assert out.startswith("Sisyphus: yyy")
    assert out.endswith("...")


def test_work_item_header_is_closing_reference() -> None:
    assert work_item_header("o/r#109") == "Closes o/r#109"


def test_edits_text_unit():
    assert prbody._edits_text("- Done: x\n") is None
    assert prbody._edits_text("- Edits to existing files: a.py: r") == "a.py: r"
    assert prbody._edits_text("- Edits to existing files:\n  - a.py: r\n- Next: y\n") == "- a.py: r"


def test_handoff_edits_no_bullet_note(tmp_path):
    p = tmp_path / "refactor-x.md"
    p.write_text("- Done: x\n", encoding="utf-8")
    assert prbody._handoff_edits(p) == (None, "Note: no edits bullet found in handoff refactor-x.md.")


def test_handoff_edits_indented_paths(tmp_path):
    p = tmp_path / "refactor-x.md"
    p.write_text(
        "- Edits to existing files:\n  - a.py: reason\n  - b.py: reason\n- Deviations from the plan: x\n",
        encoding="utf-8",
    )
    assert prbody._handoff_edits(p) == ("- a.py: reason\n  - b.py: reason", None)


def test_handoff_edits_inline_text(tmp_path):
    p = tmp_path / "refactor-x.md"
    p.write_text("- Edits to existing files: a.py: r\n  - b.py: r2\n", encoding="utf-8")
    assert prbody._handoff_edits(p) == ("a.py: r\n  - b.py: r2", None)


def test_handoff_edits_none_bullet(tmp_path):
    p = tmp_path / "refactor-x.md"
    for body in ("None", "None.", "None (no edits)"):
        p.write_text(f"- Edits to existing files: {body}\n", encoding="utf-8")
        assert prbody._handoff_edits(p) == (None, None)


def test_handoff_edits_empty_bullet(tmp_path):
    p = tmp_path / "refactor-x.md"
    p.write_text("- Edits to existing files:", encoding="utf-8")
    assert prbody._handoff_edits(p) == (None, None)


def test_handoff_edits_unreadable(tmp_path):
    note = "Note: cannot read handoff refactor-x.md."
    assert prbody._handoff_edits(tmp_path / "refactor-x.md") == (None, note)
    p = tmp_path / "refactor-x.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    assert prbody._handoff_edits(p) == (None, note)


def _rh(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_refactor_handoffs_both_bullets(tmp_path):
    p = _rh(
        tmp_path,
        "refactor-fastapi.md",
        "- Deviations from the plan: split the router\n- Edits to existing files:\n  - app/main.py: import the new router\n",
    )
    [r] = prbody.extract_refactor_handoffs([p])
    assert r == prbody.RefactorHandoff(
        "fastapi", "- fastapi: split the router", "- app/main.py: import the new router", ()
    )


def test_refactor_handoffs_no_edits_bullet(tmp_path):
    p = _rh(tmp_path, "refactor-a.md", "- Deviations from the plan: x\n")
    [r] = prbody.extract_refactor_handoffs([p])
    assert r.deviation == "- a: x"
    assert r.edits is None
    assert r.notes == ("Note: no edits bullet found in handoff refactor-a.md.",)


def test_refactor_handoffs_none_edits(tmp_path):
    p = _rh(tmp_path, "refactor-a.md", "- Deviations from the plan: x\n- Edits to existing files: None\n")
    [r] = prbody.extract_refactor_handoffs([p])
    assert r.edits is None
    assert r.notes == ()


def test_refactor_handoffs_no_deviations_bullet(tmp_path):
    p = _rh(tmp_path, "refactor-a.md", "- Edits to existing files: a.py: r\n")
    [r] = prbody.extract_refactor_handoffs([p])
    assert r.deviation is None
    assert r.edits == "a.py: r"
    assert r.notes == ("Note: no deviations bullet found in handoff refactor-a.md.",)
    q = _rh(tmp_path, "refactor-b.md", "nothing\n")
    [r2] = prbody.extract_refactor_handoffs([q])
    assert r2.notes == (
        "Note: no deviations bullet found in handoff refactor-b.md.",
        "Note: no edits bullet found in handoff refactor-b.md.",
    )


def test_refactor_handoffs_unreadable(tmp_path):
    note = ("Note: cannot read handoff refactor-x.md.",)
    [r] = prbody.extract_refactor_handoffs([tmp_path / "refactor-x.md"])
    assert (r.deviation, r.edits, r.notes) == (None, None, note)
    p = tmp_path / "refactor-x.md"
    p.write_bytes(b"\xff\xfe\x00bad")
    [r] = prbody.extract_refactor_handoffs([p])
    assert (r.deviation, r.edits, r.notes) == (None, None, note)


def test_refactor_handoffs_set_name(tmp_path):
    names = ["refactor-react.md", "refactor-refactor-x.md", "notes.md", "refactor-x", "refactor-x.MD"]
    sub = tmp_path / "refactor-dir"
    sub.mkdir()
    got = [r.set for r in prbody.extract_refactor_handoffs([sub / n for n in names])]
    assert got == ["react", "refactor-x", "notes", "x", "x.MD"]


def test_refactor_handoffs_order(tmp_path):
    paths = [_rh(tmp_path, f"refactor-{n}.md", "") for n in "cab"]
    assert [r.set for r in prbody.extract_refactor_handoffs(paths)] == ["c", "a", "b"]


def test_refactor_handoffs_empty():
    assert prbody.extract_refactor_handoffs([]) == []


def test_refactor_handoffs_deviation_none(tmp_path):
    p = _rh(tmp_path, "refactor-a.md", "- Deviations from the plan: None\n- Edits to existing files: a.py: r\n")
    [r] = prbody.extract_refactor_handoffs([p])
    assert r.deviation is None
    assert r.notes == ()


def test_refactor_handoffs_is_namedtuple(tmp_path):
    p = _rh(tmp_path, "refactor-a.md", "")
    [r] = prbody.extract_refactor_handoffs([p])
    assert isinstance(r, tuple)
    assert r._fields == ("set", "deviation", "edits", "notes")
    assert isinstance(r.notes, tuple)


def _record(**over: object) -> dict[str, object]:
    base: dict[str, object] = {"set": "fastapi", "files": ["a.py"], "evidence": ["x: 1"], "fallback": False}
    base.update(over)
    return base


def test_refactor_record_lines_fallback_yes() -> None:
    ev = ["pyproject.toml: fastapi>=0.1", "app/main.py: from fastapi import FastAPI"]
    lines = _refactor_record_lines(_record(set="generic", evidence=ev, fallback=True))
    assert "- Fallback: yes" in lines
    assert all(f"  - {e}" in lines for e in ev)


def test_refactor_record_lines_fallback_no() -> None:
    lines = _refactor_record_lines(_record())
    assert "- Fallback: no" in lines
    assert "- Fallback: yes" not in lines


def test_refactor_record_lines_empty_evidence() -> None:
    lines = _refactor_record_lines(_record(evidence=[]))
    assert "- Evidence: none" in lines
    assert not any(line.startswith("  - ") for line in lines[lines.index("- Evidence: none") :])


def test_refactor_record_lines_ignores_extra_keys() -> None:
    lines = _refactor_record_lines(_record(tests=["tests/test_a.py"], head_before="abc1234"))
    assert not any("tests/test_a.py" in line or "abc1234" in line for line in lines)


def test_refactor_record_lines_empty_files() -> None:
    assert "- Files: none" in _refactor_record_lines(_record(files=[]))


def test_refactor_record_lines_keeps_order_and_inputs() -> None:
    files, ev = ["b.py", "a.py"], ["z: 1", "a: 2"]
    lines = _refactor_record_lines(_record(files=files, evidence=ev))
    assert lines[3:5] == ["  - b.py", "  - a.py"]
    assert lines[6:8] == ["  - z: 1", "  - a: 2"]
    assert files == ["b.py", "a.py"]
    assert ev == ["z: 1", "a: 2"]


def test_refactor_record_lines_full_layout() -> None:
    rec = _record(files=["a.py", "b.py"], evidence=["pyproject.toml: fastapi"])
    assert _refactor_record_lines(rec) == [
        "### fastapi",
        "",
        "- Files:",
        "  - a.py",
        "  - b.py",
        "- Evidence:",
        "  - pyproject.toml: fastapi",
        "- Fallback: no",
    ]


def test_refactor_record_lines_deterministic() -> None:
    rec = _record()
    assert _refactor_record_lines(rec) == _refactor_record_lines(rec)


def _handoff_result(deviation: str | None = "x", edits: str | None = "e", notes: list[str] | None = None) -> dict:
    return {"set": "generic", "deviation": deviation, "edits": edits, "notes": notes if notes is not None else []}


def test_refactor_handoff_lines_both_bullets() -> None:
    result = _handoff_result("Kept the helper in place.", "- src/app/routes.py: import the new module")
    assert _refactor_handoff_lines("generic", result) == [
        "- Deviations: Kept the helper in place.",
        "- Edits to existing files: - src/app/routes.py: import the new module",
    ]


def test_refactor_handoff_lines_edits_none_note_verbatim() -> None:
    note = "Note: no edits bullet found in handoff refactor-generic.md."
    lines = _refactor_handoff_lines("generic", _handoff_result("x", None, [note]))
    assert len(lines) == 3
    assert lines[1] == "- Edits to existing files: unknown (see note)"
    assert lines[2] == note


def test_refactor_handoff_lines_none_result() -> None:
    lines = _refactor_handoff_lines("fastapi", None)
    assert lines == ["Note: no handoff found for set fastapi."]
    assert not any(x.startswith(("- Deviations:", "- Edits to existing files:")) for x in lines)


def test_refactor_handoff_lines_deviation_none() -> None:
    assert _refactor_handoff_lines("generic", _handoff_result(None, "e")) == [
        "- Deviations: none",
        "- Edits to existing files: e",
    ]


def test_refactor_handoff_lines_notes_order() -> None:
    lines = _refactor_handoff_lines("generic", _handoff_result(notes=["Note: a", "Note: b"]))
    assert lines[2:] == ["Note: a", "Note: b"]
    assert lines[:2] == ["- Deviations: x", "- Edits to existing files: e"]


def test_refactor_handoff_lines_multiline() -> None:
    lines = _refactor_handoff_lines("generic", _handoff_result("line one\nline two"))
    assert lines[:2] == ["- Deviations: line one", "line two"]
    assert "- Deviations: line one\nline two" in "\n".join(lines)


def test_refactor_handoff_lines_empty_text_and_empty_note() -> None:
    lines = _refactor_handoff_lines("generic", _handoff_result("", "e", [""]))
    assert lines == ["- Deviations:", "- Edits to existing files: e", ""]


def test_refactor_handoff_lines_deterministic_and_non_mutating() -> None:
    import copy

    result = _handoff_result(notes=["Note: a"])
    before = copy.deepcopy(result)
    first = _refactor_handoff_lines("generic", result)
    assert first == _refactor_handoff_lines("generic", result)
    assert result == before
    assert first is not result["notes"]


def test_refactor_handoff_lines_does_not_embed() -> None:
    lines = _refactor_handoff_lines("generic", _handoff_result("ping @someone see #12"))
    assert lines[0] == "- Deviations: ping @someone see #12"


def test_format_refactor_section_keeps_input_order() -> None:
    records = [_record(set="react"), _record(set="generic"), _record(set="fastapi")]
    results = {str(r["set"]): _handoff_result() for r in records}
    text = format_refactor_section(records, results)
    heads = [line for line in text.split("\n") if line.startswith("### ")]
    assert heads == ["### react", "### generic", "### fastapi"]


def test_format_refactor_section_empty() -> None:
    assert format_refactor_section([], {}) == ""


def test_format_refactor_section_deterministic() -> None:
    records = [_record(set="react"), _record(set="generic")]
    results = {"react": _handoff_result()}
    assert format_refactor_section(records, results) == format_refactor_section(records, results)


def test_format_refactor_section_missing_handoff() -> None:
    text = format_refactor_section([_record(set="react")], {})
    assert "Note: no handoff found for set react." in text
    assert "### react" in text
    for label in ("- Files:", "- Evidence:", "- Fallback:"):
        assert label in text


def test_format_refactor_section_layout() -> None:
    records = [_record(set="react"), _record(set="fastapi")]
    results = {str(r["set"]): _handoff_result() for r in records}
    text = format_refactor_section(records, results)
    assert text.count("\n\n### ") == 1
    assert "\n\n\n" not in text
    assert text.startswith("### react")
    assert not text.startswith("## Refactor")
    assert not text.endswith("\n")


def test_format_refactor_section_ignores_extra_keys() -> None:
    plain = _record(set="react")
    extra = _record(set="react", tests=["t.py"], head_before="abc")
    results = {"react": _handoff_result()}
    assert format_refactor_section([extra], results) == format_refactor_section([plain], results)


def test_format_refactor_section_does_not_mutate() -> None:
    import copy

    records = [_record(set="react"), _record(set="generic")]
    results = {"react": _handoff_result(notes=["Note: a"])}
    before = (copy.deepcopy(records), copy.deepcopy(results))
    format_refactor_section(records, results)
    assert (records, results) == before


def _compose_refactor(tmp_path: Path, **kw: Any) -> str:
    ws = tmp_path / "ws"
    return compose_body("o/r#109", workspace=ws, plan_dir=ws / "plan", home=tmp_path, **kw)


def test_compose_refactor_omitted_is_identical(tmp_path: Path) -> None:
    base = {"summary": "S", "phases": "P", "decisions": "D", "deviations": "V", "requirements": "R"}
    expected = _compose_refactor(tmp_path, **base)
    assert _compose_refactor(tmp_path, refactor="", **base) == expected
    assert _compose_refactor(tmp_path, refactor="  \n\t", **base) == expected
    assert "## Refactor" not in expected
    assert expected == (
        "Closes o/r#109\n\n## Summary\n\nS\n\n## Phases\n\nP\n\n## Decisions\n\nD\n\n## Deviations\n\nV"
        "\n\n" + prbody.requirements_block("R")
    )


def test_compose_refactor_renders_section(tmp_path: Path) -> None:
    body = _compose_refactor(tmp_path, refactor="Set: generic", requirements="")
    assert "\n\n## Refactor\n\nSet: generic" in body


def test_compose_refactor_order(tmp_path: Path) -> None:
    body = _compose_refactor(
        tmp_path, summary="S", phases="P", decisions="D", deviations="V", refactor="F", requirements="R"
    )
    idx = [
        body.index(m) for m in ("## Summary", "## Phases", "## Decisions", "## Deviations", "## Refactor", "<details>")
    ]
    assert idx == sorted(idx)


def test_compose_refactor_embedded(tmp_path: Path) -> None:
    text = f"{tmp_path}/ws/x @user #12 </details>"
    body = _compose_refactor(tmp_path, refactor=text, deviations=text, requirements="")
    refactor_part = body.split("## Refactor\n\n", 1)[1]
    deviations_part = body.split("## Deviations\n\n", 1)[1].split("\n\n## Refactor")[0]
    assert refactor_part == deviations_part
    assert str(tmp_path) not in body and "</details>" not in body
    assert not mentions_issue(body, 12)


def test_compose_refactor_truncated_first(tmp_path: Path) -> None:
    body = _compose_refactor(
        tmp_path,
        summary="SUMMARYSTART " + "s" * 100,
        deviations="DEV",
        refactor="r" * (MAX_BODY_CHARS * 2),
        requirements="",
    )
    assert len(body) <= MAX_BODY_CHARS
    assert body.endswith(prbody.TRUNCATED_NOTE)
    assert "SUMMARYSTART" in body and "## Deviations\n\nDEV" in body
    assert "## Refactor" in body and "r" * (MAX_BODY_CHARS * 2) not in body
    body = _compose_refactor(tmp_path, summary="SUMMARYSTART " + "s" * (MAX_BODY_CHARS * 2), refactor="REFACTORTEXT")
    assert len(body) <= MAX_BODY_CHARS and body.endswith(prbody.TRUNCATED_NOTE)
    assert "SUMMARYSTART" in body and "REFACTORTEXT" not in body


def test_compose_refactor_is_keyword_only() -> None:
    import pytest

    with pytest.raises(TypeError):
        compose_body("o/r#109", "", "", "", "", "refactor", workspace=Path("."))  # ty: ignore[too-many-positional-arguments]


# --- orchestrator call site: refactor-input.json -> build_body ---


class _NoGit:
    async def __call__(self, argv: Sequence[str], cwd: Path | None) -> Any:
        from dotharness.sisyphus.subproc import CommandResult

        return CommandResult(1, "", "no git")


def _call_site_run(tmp_path: Path) -> Any:
    from dotharness.sisyphus import orchestrator
    from tests.sisyphus.test_orchestrator import ITEM, World

    w = World(tmp_path)
    return orchestrator._Run(ITEM, w.config(), w.options(), w.agent, _NoGit(), w, w.clock, w)


def _input_path(run: Any) -> Path:
    run.handoff_dir.mkdir(parents=True, exist_ok=True)
    return run.handoff_dir / "refactor-input.json"


_GENERIC_RECORD = {
    "set": "generic",
    "files": ["a.py"],
    "tests": ["tests/test_a.py"],
    "evidence": ["marker-evidence-xyz"],
    "fallback": True,
    "head_before": "a" * 40,
}


async def test_call_site_missing_file_passes_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _call_site_run(tmp_path)
    seen: dict[str, Any] = {}
    real = prbody.build_body

    async def spy(*args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(prbody, "build_body", spy)
    body = await run.build_pr_body()
    assert seen["refactor_records"] == []
    assert seen["refactor_handoff_dir"] == run.handoff_dir
    assert body != prbody.work_item_header(run.item)
    assert run.notes == []


async def test_call_site_valid_record_passed_and_rendered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _call_site_run(tmp_path)
    _input_path(run).write_text(json.dumps([_GENERIC_RECORD]), encoding="utf-8")
    seen: dict[str, Any] = {}
    real = prbody.build_body

    async def spy(*args: Any, **kwargs: Any) -> str:
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(prbody, "build_body", spy)
    body = await run.build_pr_body()
    assert seen["refactor_records"] == [_GENERIC_RECORD]
    assert seen["refactor_handoff_dir"] == run.handoff_dir
    assert "marker-evidence-xyz" in body
    assert "Refactor" in body


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe", b"{not json", b"{}", b'"x"', b"3", b"null", b"true", b"[" * 100000, b"[]"],
)
async def test_call_site_unusable_file_leaves_body_unchanged(tmp_path: Path, content: bytes) -> None:
    run = _call_site_run(tmp_path)
    baseline = await run.build_pr_body()
    _input_path(run).write_bytes(content)
    assert await run.build_pr_body() == baseline
    assert baseline != prbody.work_item_header(run.item)
    assert run.notes == []


async def test_call_site_directory_and_permission_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _call_site_run(tmp_path)
    baseline = await run.build_pr_body()
    _input_path(run).mkdir()
    assert await run.build_pr_body() == baseline
    _input_path(run).rmdir()
    _input_path(run).write_text("[]", encoding="utf-8")

    real_read = Path.read_text

    def deny(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "refactor-input.json":
            raise PermissionError("denied")
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", deny)
    assert await run.build_pr_body() == baseline
    assert run.notes == []


def test_read_refactor_records_returns_list_unchanged(tmp_path: Path) -> None:
    from dotharness.sisyphus.orchestrator import _read_refactor_records

    path = tmp_path / "refactor-input.json"
    path.write_text(json.dumps([1, "x", {"a": None}, []]), encoding="utf-8")
    assert _read_refactor_records(path) == [1, "x", {"a": None}, []]
    assert _read_refactor_records(tmp_path / "missing.json") == []


def test_refactor_text_degrades_on_malformed_records() -> None:
    from dotharness.sisyphus.prbody import _refactor_text

    bad = [
        {"set": "generic"},
        {"set": "str-files", "files": "a.py", "evidence": [], "fallback": False},
    ]
    text = _refactor_text(bad, None)
    assert "Note: malformed selection record for generic." in text
    assert "Note: malformed selection record for str-files." in text
    good = {"set": "ok", "files": ["a.py"], "evidence": [], "fallback": False}
    mixed = _refactor_text([bad[0], good], None)
    assert "### ok" in mixed
    assert "malformed selection record for generic" in mixed
