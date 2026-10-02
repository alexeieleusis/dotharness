import pytest

from harness import state


def test_read_vibe_heal_defaults(tmp_xdg):
    result = state.read_vibe_heal_state("acme-frontend")
    assert result == {"version": 1, "reviewed_shas": {}, "last_main_sha": ""}


def test_write_then_read_vibe_heal(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_main_sha"] == "abc123"
    assert result["version"] == 1


def test_read_self_review_defaults(tmp_xdg):
    result = state.read_self_review_state("acme-frontend")
    assert result == {
        "version": 1,
        "reviewed_prs": [],
        "partial_reviews": {},
        "design_reviewed_prs": [],
        "traceability_reviewed_prs": [],
        "regret_reviewed_prs": [],
    }


def test_write_then_read_self_review(tmp_xdg):
    state.write_self_review_state("acme-frontend", [1, 2, 3])
    result = state.read_self_review_state("acme-frontend")
    assert result["reviewed_prs"] == [1, 2, 3]


def test_atomic_write_no_tmp_left(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="deadbeef")
    leftovers = list((tmp_xdg / "state" / "acme-frontend").glob("*.tmp"))
    assert leftovers == []


def test_delete_state_review_prs(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 10, "sha1", 1000.0)
    state.delete_state("acme-frontend", "review-prs")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {}  # reset to default


def test_delete_state_unknown_command_raises(tmp_xdg):
    with pytest.raises(ValueError, match="No state file"):
        state.delete_state("acme-frontend", "address-comments")


def test_write_last_main_sha_preserves_reviewed_shas(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"7": {"sha": "sha7", "reviewed_at": 1000.0}}
    assert result["last_main_sha"] == "abc123"


def test_record_reviewed_sha_preserves_last_main_sha(tmp_xdg):
    state.write_vibe_heal_state("acme-frontend", last_main_sha="abc123")
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"7": {"sha": "sha7", "reviewed_at": 1000.0}}
    assert result["last_main_sha"] == "abc123"


def test_get_reviewed_sha_returns_none_when_absent(tmp_xdg):
    assert state.get_reviewed_sha("acme-frontend", 99) is None


def test_get_reviewed_sha_returns_recorded_value(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    assert state.get_reviewed_sha("acme-frontend", 7) == "sha7"


def test_record_reviewed_sha_overwrites_existing_entry(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.record_reviewed_sha("acme-frontend", 7, "sha7-new", 2000.0)
    assert state.get_reviewed_sha("acme-frontend", 7) == "sha7-new"


def test_record_reviewed_sha_stores_reviewed_at(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1234.5)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"]["7"]["reviewed_at"] == 1234.5


def test_record_reviewed_sha_overwrites_reviewed_at(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.record_reviewed_sha("acme-frontend", 7, "sha7-new", 2000.0)
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"]["7"]["reviewed_at"] == 2000.0


def test_prune_reviewed_shas_drops_closed_prs(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.record_reviewed_sha("acme-frontend", 9, "sha9", 1000.0)
    state.prune_reviewed_shas("acme-frontend", {9})
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"9": {"sha": "sha9", "reviewed_at": 1000.0}}


def test_prune_reviewed_shas_no_op_when_nothing_changes(tmp_xdg):
    state.record_reviewed_sha("acme-frontend", 7, "sha7", 1000.0)
    state.prune_reviewed_shas("acme-frontend", {7})
    leftovers = list((tmp_xdg / "state" / "acme-frontend").glob("*.tmp"))
    assert leftovers == []
    assert state.read_vibe_heal_state("acme-frontend")["reviewed_shas"] == {"7": {"sha": "sha7", "reviewed_at": 1000.0}}


def test_read_vibe_heal_state_migrates_legacy_string_entries(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "reviewed_shas": {"7": "legacy-sha"}, "last_main_sha": ""}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {"7": {"sha": "legacy-sha", "reviewed_at": 0}}


def test_read_vibe_heal_state_defaults_missing_last_main_sha(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "reviewed_shas": {}}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["last_main_sha"] == ""


def test_read_vibe_heal_state_defaults_missing_reviewed_shas(tmp_xdg):
    import json

    path = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "last_main_sha": "abc"}))
    result = state.read_vibe_heal_state("acme-frontend")
    assert result["reviewed_shas"] == {}


def test_read_vibe_heal_corrupted_json_fallback(tmp_xdg, caplog):
    state_file = tmp_xdg / "state" / "acme-frontend" / "vibe_heal.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "last_pr", }')
    result = state.read_vibe_heal_state("acme-frontend")
    assert result == {"version": 1, "reviewed_shas": {}, "last_main_sha": ""}
    assert "Corrupted state file" in caplog.text


def test_prune_self_review_state_drops_closed_prs(tmp_xdg):
    state.write_self_review_state("acme-frontend", [7, 9])
    state.prune_self_review_state("acme-frontend", {9})
    result = state.read_self_review_state("acme-frontend")
    assert result["reviewed_prs"] == [9]


def test_prune_self_review_state_drops_partial_reviews_too(tmp_xdg):
    state.set_partial_reviewed_files("acme-frontend", 7, ["a.py"])
    state.set_partial_reviewed_files("acme-frontend", 9, ["b.py"])
    state.prune_self_review_state("acme-frontend", {9})
    result = state.read_self_review_state("acme-frontend")
    assert result["partial_reviews"] == {"9": ["b.py"]}


def test_prune_self_review_state_no_op_when_nothing_changes(tmp_xdg, monkeypatch):
    state.write_self_review_state("acme-frontend", [7])
    calls = []
    monkeypatch.setattr(state, "_atomic_write", lambda *a: calls.append(a))
    state.prune_self_review_state("acme-frontend", {7})
    assert calls == []
    assert state.read_self_review_state("acme-frontend")["reviewed_prs"] == [7]


def test_add_design_reviewed_pr_is_independent_of_reviewed_prs(tmp_xdg):
    # reviewed_prs is untouched by design_reviewed_prs — the two are tracked
    # separately so a design-pass retry never forces (or is forced by) the
    # per-file review's retry-from-scratch behavior.
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 9)
    assert state.get_design_reviewed_prs("acme-frontend") == {9}
    assert state.read_self_review_state("acme-frontend")["reviewed_prs"] == [7]


def test_add_design_reviewed_pr_is_idempotent(tmp_xdg, monkeypatch):
    state.add_design_reviewed_pr("acme-frontend", 9)
    calls = []
    monkeypatch.setattr(state, "_atomic_write", lambda *a: calls.append(a))
    state.add_design_reviewed_pr("acme-frontend", 9)
    assert calls == []
    assert state.get_design_reviewed_prs("acme-frontend") == {9}


def test_prune_self_review_state_drops_design_reviewed_prs_too(tmp_xdg):
    state.add_design_reviewed_pr("acme-frontend", 7)
    state.add_design_reviewed_pr("acme-frontend", 9)
    state.prune_self_review_state("acme-frontend", {9})
    assert state.get_design_reviewed_prs("acme-frontend") == {9}


def test_read_self_review_corrupted_json_fallback(tmp_xdg, caplog):
    state_file = tmp_xdg / "state" / "acme-frontend" / "self_review.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text('{"version": 1, "reviewed_prs": [1, 2')
    result = state.read_self_review_state("acme-frontend")
    assert result == {
        "version": 1,
        "reviewed_prs": [],
        "partial_reviews": {},
        "design_reviewed_prs": [],
        "traceability_reviewed_prs": [],
        "regret_reviewed_prs": [],
    }
    assert "Corrupted state file" in caplog.text


def test_add_traceability_reviewed_pr_is_independent_of_other_state(tmp_xdg):
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 8)
    state.add_traceability_reviewed_pr("acme-frontend", 9)
    assert state.get_traceability_reviewed_prs("acme-frontend") == {9}
    assert state.get_design_reviewed_prs("acme-frontend") == {8}
    assert state.read_self_review_state("acme-frontend")["reviewed_prs"] == [7]


def test_add_traceability_reviewed_pr_is_idempotent(tmp_xdg, monkeypatch):
    state.add_traceability_reviewed_pr("acme-frontend", 9)
    calls = []
    monkeypatch.setattr(state, "_atomic_write", lambda *a: calls.append(a))
    state.add_traceability_reviewed_pr("acme-frontend", 9)
    assert calls == []
    assert state.get_traceability_reviewed_prs("acme-frontend") == {9}


def test_prune_self_review_state_drops_traceability_reviewed_prs_too(tmp_xdg):
    state.add_traceability_reviewed_pr("acme-frontend", 7)
    state.add_traceability_reviewed_pr("acme-frontend", 9)
    state.prune_self_review_state("acme-frontend", {9})
    assert state.get_traceability_reviewed_prs("acme-frontend") == {9}


def test_add_regret_reviewed_pr_is_independent_of_other_state(tmp_xdg):
    # regret_reviewed_prs is untouched by — and does not touch — the other three lists:
    # a regret-pass retry never forces (or is forced by) any other pass's retry.
    state.write_self_review_state("acme-frontend", [7])
    state.add_design_reviewed_pr("acme-frontend", 8)
    state.add_traceability_reviewed_pr("acme-frontend", 85)
    state.add_regret_reviewed_pr("acme-frontend", 9)
    assert state.get_regret_reviewed_prs("acme-frontend") == {9}
    assert state.get_design_reviewed_prs("acme-frontend") == {8}
    assert state.get_traceability_reviewed_prs("acme-frontend") == {85}
    assert state.read_self_review_state("acme-frontend")["reviewed_prs"] == [7]


def test_add_regret_reviewed_pr_is_idempotent(tmp_xdg, monkeypatch):
    state.add_regret_reviewed_pr("acme-frontend", 9)
    calls = []
    monkeypatch.setattr(state, "_atomic_write", lambda *a: calls.append(a))
    state.add_regret_reviewed_pr("acme-frontend", 9)
    assert calls == []
    assert state.get_regret_reviewed_prs("acme-frontend") == {9}


def test_prune_self_review_state_drops_regret_reviewed_prs_too(tmp_xdg):
    state.add_regret_reviewed_pr("acme-frontend", 7)
    state.add_regret_reviewed_pr("acme-frontend", 9)
    state.prune_self_review_state("acme-frontend", {9})
    assert state.get_regret_reviewed_prs("acme-frontend") == {9}


# --- local-review: state reset, CLI, scheduler -------------------------------------------------

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest.mock import patch  # noqa: E402

from click.testing import CliRunner  # noqa: E402

from harness.cli import cli  # noqa: E402
from harness.config import load_config  # noqa: E402


def _lr_config(tmp_path: Path, repo: Path, extra: str = "") -> Path:
    kd = tmp_path / "knowledge"
    (kd / "pr-review").mkdir(parents=True, exist_ok=True)
    for name in ("local-review-file.md", "local-review-summary.md", "local-review-design.md"):
        (kd / "pr-review" / name).write_text("T {OUTPUT_FILE}", encoding="utf-8")
    cfg = tmp_path / "lr.toml"
    cfg.write_text(
        f'[harness]\nknowledge_dir = "{kd}"\n[repo]\nworking_dir = "{repo}"\n'
        f'[local_review]\noutput_dir = "{tmp_path / "out"}"\n{extra}'
    )
    return cfg


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "base.txt").write_text("b")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "checkout", "-b", "feature/x")
    (repo / "a.py").write_text("print(1)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "feat")
    return repo


def test_state_reset_local_review_deletes_slug_dir_only(tmp_xdg, tmp_path):
    repo = _make_repo(tmp_path)
    cfg_path = _lr_config(tmp_path, repo)
    slug = load_config(cfg_path, require_repo_name=False).repo_slug
    out = tmp_path / "out"
    (out / slug / "feature" / "x" / "abc").mkdir(parents=True)
    (out / slug / "feature" / "x" / "abc" / "manifest.json").write_text("{}")
    (out / "other-slug").mkdir()
    r = CliRunner().invoke(cli, ["state", "reset", "local-review", "--config", str(cfg_path), "--yes"])
    assert r.exit_code == 0, r.output
    assert not (out / slug).exists()
    assert (out / "other-slug").is_dir()


def test_state_reset_local_review_prompt_names_dir_and_abort_keeps(tmp_xdg, tmp_path):
    repo = _make_repo(tmp_path)
    cfg_path = _lr_config(tmp_path, repo)
    slug = load_config(cfg_path, require_repo_name=False).repo_slug
    target = tmp_path / "out" / slug
    target.mkdir(parents=True)
    r = CliRunner().invoke(cli, ["state", "reset", "local-review", "--config", str(cfg_path)], input="n\n")
    assert r.exit_code != 0
    assert str(target) in r.output
    assert target.is_dir()


def test_state_reset_local_review_missing_dir_is_noop(tmp_xdg, tmp_path):
    repo = _make_repo(tmp_path)
    cfg_path = _lr_config(tmp_path, repo)
    r = CliRunner().invoke(cli, ["state", "reset", "local-review", "--config", str(cfg_path), "--yes"])
    assert r.exit_code == 0
    assert "Nothing to reset" in r.output


def test_state_reset_other_commands_unchanged(tmp_xdg, minimal_toml):
    state.record_reviewed_sha("acme-frontend", 10, "sha1", 1000.0)
    r = CliRunner().invoke(cli, ["state", "reset", "review-prs", "--config", str(minimal_toml), "--yes"])
    assert r.exit_code == 0
    assert state.read_vibe_heal_state("acme-frontend")["reviewed_shas"] == {}


class _StubBackend:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, prompt, cwd, opencode_dir=None, context=None):
        import re

        match = re.search(r"(/\S+\.md)", prompt)
        assert match
        out = Path(match.group(1))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("No findings.\n", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")


def test_local_review_cli_end_to_end_no_network(tmp_xdg, tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)  # no remotes
    cfg_path = _lr_config(tmp_path, repo)  # no repo.name
    monkeypatch.setenv("PATH", "/usr/bin:/bin")  # gh absent
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with patch("harness.runners.local_review.Backend", _StubBackend):
        r = CliRunner().invoke(cli, ["run", "--config", str(cfg_path), "local-review"])
    assert r.exit_code == 0, r.output
    assert "per-file: 1/1 done" in r.output
    assert "summary: done" in r.output
    assert "design: done" in r.output
    assert "Base: main" in r.output
    assert "Head: " in r.output and "Review directory: " in r.output


def test_local_review_cli_precondition_failure_exits_nonzero(tmp_xdg, tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("dirty\n")
    cfg_path = _lr_config(tmp_path, repo)
    r = CliRunner().invoke(cli, ["run", "--config", str(cfg_path), "local-review"])
    assert r.exit_code != 0


def test_local_review_cli_nothing_to_review_exits_zero(tmp_xdg, tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "checkout", "main")
    cfg_path = _lr_config(tmp_path, repo)
    r = CliRunner().invoke(cli, ["run", "--config", str(cfg_path), "local-review"])
    assert r.exit_code == 0
    assert "Nothing to review" in r.output


def test_local_review_cli_failed_pass_exits_nonzero(tmp_xdg, tmp_path):
    repo = _make_repo(tmp_path)
    cfg_path = _lr_config(tmp_path, repo)

    class _Failing(_StubBackend):
        def run(self, prompt, cwd, opencode_dir=None, context=None):
            return subprocess.CompletedProcess([], 1, "", "boom")

    with patch("harness.runners.local_review.Backend", _Failing):
        r = CliRunner().invoke(cli, ["run", "--config", str(cfg_path), "local-review"])
    assert r.exit_code != 0
    assert "per-file: 0/1 done" in r.output


def test_local_review_not_in_run_all(tmp_xdg, minimal_toml):
    with (
        patch("harness.runners.local_review.run_local_review") as lr,
        patch("harness.runners.review_prs.run"),
        patch("harness.runners.focused_review.run"),
        patch("harness.runners.self_review.run"),
        patch("harness.runners.review_requested.run"),
        patch("harness.runners.address_comments.run"),
    ):
        CliRunner().invoke(cli, ["run", "--config", str(minimal_toml), "all"])
    lr.assert_not_called()


def test_schedule_accepts_local_review(tmp_xdg, minimal_toml):
    with patch("harness.scheduler.install_cron") as install:
        r = CliRunner().invoke(
            cli, ["schedule", "install", "local-review", "--config", str(minimal_toml), "--every", "2h"]
        )
    assert r.exit_code == 0, r.output
    entry = install.call_args[0][0]
    assert entry.command == "local-review"
    assert "run --config" in entry.to_cron_line() and "local-review" in entry.to_cron_line()
    with patch("harness.scheduler.uninstall_cron") as uninstall:
        r = CliRunner().invoke(cli, ["schedule", "uninstall", "local-review", "--config", str(minimal_toml)])
    assert r.exit_code == 0
    uninstall.assert_called_once()
