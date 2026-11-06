"""Contract: sisyphus reads what ``plan draft-phases`` writes as data, never importing spec_prism_flow.

The producer runs in a subprocess; the ``[build]`` table of ``.spec-prism-flow.toml`` is sisyphus's own and
spec-prism-flow must ignore it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from dotharness.sisyphus import plan, prbody
from tests.sisyphus.conftest import forbidden_imports

SRC = Path(__file__).parents[2] / "src" / "dotharness" / "sisyphus"


def _leaf(path: str, name: str, scope: str) -> dict:
    doc = f"### Goals\n- **G1.** Do {name}.\n\n### Non-goals\n- Not this.\n"
    chunk = {"path": path, "name": name, "file_scope_estimate": [scope], "requirements_slice": "s", "depth": 1}
    return {"chunk": chunk, "leaf_doc": doc}


def _draft(tmp_path: Path) -> Path:
    ws, phases = tmp_path / "ws", tmp_path / "phases"
    ws.mkdir()
    root = {
        "chunk": {"path": "A", "name": "root", "file_scope_estimate": [], "requirements_slice": "s", "depth": 0},
        "children": [_leaf("A-1", "alpha", "a.py"), _leaf("A-2", "beta", "b.py")],
    }
    (ws / "decompose_tree.json").write_text(json.dumps(root))
    cfg = tmp_path / ".spec-prism-flow.toml"
    cfg.write_text(
        f'[plan]\nworkspace_dir = "{ws}"\nphase_dir = "{phases}"\n\n[build]\nsome_key = "owned by sisyphus"\n'
    )
    proc = subprocess.run(
        [sys.executable, "-m", "dotharness.spec_prism_flow.cli", "plan", "draft-phases", "--config", str(cfg), "--yes"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return cfg


def test_draft_phases_output_is_readable_as_data(tmp_path: Path) -> None:
    cfg = _draft(tmp_path)  # exit 0 also proves the [build] table is ignored
    spf = plan.read_spf_config(cfg)
    assert spf is not None
    assert spf["build"] == {"some_key": "owned by sisyphus"}
    phase_dir = plan.plan_path(spf, tmp_path, "phase_dir")
    assert phase_dir is not None
    stems = ["01-alpha-leaf", "02-beta-leaf"]
    assert sorted(p.stem for p in phase_dir.glob(plan.LEAF_GLOB)) == stems
    assert plan.leaf_stems(phase_dir) == stems  # fallback: no graph.json yet
    text = (phase_dir / "01-alpha-leaf.md").read_text()
    for header in (
        "## Scope",
        "## Requirements",
        "## Acceptance criteria",
        "## Manual test checklist",
        "## Depends on",
    ):
        assert header in text
    assert "- a.py" in text


def test_graph_json_nodes_win_over_glob(tmp_path: Path) -> None:
    cfg = _draft(tmp_path)
    phase_dir = plan.plan_path(plan.read_spf_config(cfg), tmp_path, "phase_dir")
    assert phase_dir is not None
    (phase_dir / "graph.json").write_text(json.dumps({"nodes": ["02-beta-leaf", "01-alpha-leaf"], "edges": []}))
    assert plan.leaf_stems(phase_dir) == ["02-beta-leaf", "01-alpha-leaf"]
    assert "02-beta-leaf" in prbody.list_phases(phase_dir, "")


def test_sisyphus_modules_never_import_spec_prism_flow() -> None:
    assert not any(forbidden_imports(p.read_text()) for p in SRC.glob("*.py"))
