from __future__ import annotations

import ast
import json
from pathlib import Path

from sisyphus import prbody
from sisyphus.pick import mentions_issue
from sisyphus.prbody import (
    HEADER_NOTICE,
    MAX_BODY_CHARS,
    PLAN_NOT_MATCHED_NOTE,
    commit_log_args,
    compose_body,
    extract_decisions,
    extract_deviations,
    extract_summary,
    format_decisions,
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
)

STEMS = ["01-alpha-leaf", "02-beta-leaf"]


def test_module_imports_neither_harness_nor_spec_prism() -> None:
    tree = ast.parse(Path(prbody.__file__).read_text())
    mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in mods if "harness" in m or "spec_prism" in m]
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
    assert body.startswith(f"Work item: o/r#109\n\n{HEADER_NOTICE}\n\n## Summary")
    idx = [body.index(m) for m in ("## Summary", "## Phases", "## Decisions", "## Deviations", "<details>")]
    assert idx == sorted(idx)
    assert body.count("</details>") == 1 and body.endswith("</details>")
    assert str(tmp_path) not in body
    assert "<summary>Full requirements</summary>\n\n# Req" in body
    assert not mentions_issue(body, 109) and not mentions_issue(body, 12)
    assert (
        compose_body("o/r#109", workspace=tmp_path)
        == f"Work item: o/r#109\n\n{HEADER_NOTICE}\n\nNote: `requirements.md` is missing or unreadable; full requirements omitted."
    )


def test_compose_oversize_drops_details_first(tmp_path: Path) -> None:
    body = _compose(tmp_path, summary="short", requirements="x" * (MAX_BODY_CHARS + 10))
    assert "<details>" not in body and "short" in body
    assert "<plan-dir>/requirements.md" in body and len(body) <= MAX_BODY_CHARS


def test_compose_oversize_truncates_remaining(tmp_path: Path) -> None:
    body = _compose(tmp_path, summary="```\n" + "y" * (MAX_BODY_CHARS * 2), phases="late", requirements="r")
    assert len(body) <= MAX_BODY_CHARS
    assert body.startswith(f"Work item: o/r#109\n\n{HEADER_NOTICE}")
    assert "late" not in body and "truncated" in body and "<plan-dir>/requirements.md" in body
    assert body.count("```") % 2 == 0


def test_read_requirements_degrades(tmp_path: Path) -> None:
    assert read_requirements(None) is None
    assert read_requirements(tmp_path) is None
    (tmp_path / "requirements.md").write_bytes(b"\xff\xfe")
    assert read_requirements(tmp_path) is None
    (tmp_path / "requirements.md").write_text("ok")
    assert read_requirements(tmp_path) == "ok"
