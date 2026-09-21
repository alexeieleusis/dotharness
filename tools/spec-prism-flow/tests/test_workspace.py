import json

from spec_prism_flow.workspace import build_manifest, init_workspace, manifest_path


def test_manifest_path(tmp_path):
    workspace_dir = tmp_path / "some-workspace"

    assert manifest_path(workspace_dir) == workspace_dir / "init_manifest.json"


def test_build_manifest_resolves_paths_and_defaults_optionals_to_null(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")

    manifest = build_manifest(brief, None, None, [])

    assert manifest == {
        "brief": str(brief.resolve()),
        "code": None,
        "conventions": None,
        "links": [],
    }


def test_build_manifest_resolves_code_and_conventions(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    code = tmp_path / "code"
    code.mkdir()
    conventions = tmp_path / "CONVENTIONS.md"
    conventions.write_text("conventions")

    manifest = build_manifest(brief, code, conventions, ["https://example.com/a", "https://example.com/b"])

    assert manifest["code"] == str(code.resolve())
    assert manifest["conventions"] == str(conventions.resolve())
    assert manifest["links"] == ["https://example.com/a", "https://example.com/b"]


def test_init_workspace_creates_workspace_dir_if_absent(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    workspace_dir = tmp_path / "workspace"

    written = init_workspace(workspace_dir, brief, None, None, [])

    assert workspace_dir.is_dir()
    assert written == workspace_dir / "init_manifest.json"


def test_init_workspace_never_creates_other_directories(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    workspace_dir = tmp_path / "workspace"

    init_workspace(workspace_dir, brief, None, None, [])

    assert set(tmp_path.iterdir()) == {brief, workspace_dir}
    assert list(workspace_dir.iterdir()) == [workspace_dir / "init_manifest.json"]


def test_init_workspace_writes_manifest_with_resolved_paths(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    conventions = tmp_path / "CONVENTIONS.md"
    conventions.write_text("conventions")
    workspace_dir = tmp_path / "workspace"

    written = init_workspace(workspace_dir, brief, None, conventions, ["https://example.com"])
    data = json.loads(written.read_text())

    assert data == {
        "brief": str(brief.resolve()),
        "code": None,
        "conventions": str(conventions.resolve()),
        "links": ["https://example.com"],
    }


def test_init_workspace_overwrites_existing_manifest(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("brief")
    other_brief = tmp_path / "other_brief.md"
    other_brief.write_text("other brief")
    workspace_dir = tmp_path / "workspace"

    init_workspace(workspace_dir, brief, None, None, [])
    written = init_workspace(workspace_dir, other_brief, None, None, [])
    data = json.loads(written.read_text())

    assert data["brief"] == str(other_brief.resolve())
