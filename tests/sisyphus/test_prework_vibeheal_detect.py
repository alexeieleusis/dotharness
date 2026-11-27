"""vibe_heal configured detector: file reads only."""

from __future__ import annotations

from pathlib import Path

import pytest

from dotharness.sisyphus import prework_inputs
from dotharness.sisyphus.prework_inputs import _is_configured, vibeheal_configured

ALL_VARS = "SONARQUBE_URL=u\nSONARQUBE_TOKEN=t\nSONARQUBE_PROJECT_KEY=k\n"
ENV_ALL = {"SONARQUBE_URL": "u", "SONARQUBE_TOKEN": "t", "SONARQUBE_PROJECT_KEY": "k"}


def touch(p: Path, text: str = "") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def dirs(tmp_path):
    repo = tmp_path / "repo"
    cfg = repo / "conf"
    cfg.mkdir(parents=True)
    return repo, cfg


@pytest.mark.parametrize("name", [".env.vibeheal", "env.vibeheal", ".env.vibeheal.prod", "env.vibeheal.ci", ".env"])
@pytest.mark.parametrize("where", ["repo", "cfg"])
def test_each_location_and_name(dirs, name, where):
    repo, cfg = dirs
    f = touch((repo if where == "repo" else cfg) / name, ALL_VARS)
    res = vibeheal_configured(repo, cfg, {})
    assert (res.configured, res.env_file) == (True, f)


def test_nothing_found(dirs):
    repo, cfg = dirs
    assert vibeheal_configured(repo, cfg, {}) == prework_inputs.VibeHealDetection(False, None)


def test_example_files_ignored(dirs):
    repo, cfg = dirs
    touch(repo / ".env.vibeheal.example")
    touch(repo / "env.vibeheal.example")
    assert not vibeheal_configured(repo, cfg, {}).configured


def test_name_preference_and_repo_before_config_dir(dirs):
    repo, cfg = dirs
    touch(repo / ".env", ALL_VARS)
    exact = touch(repo / "env.vibeheal")
    touch(repo / ".env.vibeheal.x")
    assert vibeheal_configured(repo, cfg, {}).env_file == exact
    touch(repo / ".env.vibeheal")
    assert vibeheal_configured(repo, cfg, {}).env_file == repo / ".env.vibeheal"
    only_cfg = touch(cfg / ".env.vibeheal")
    (repo / ".env.vibeheal").unlink()
    (repo / "env.vibeheal").unlink()
    (repo / ".env.vibeheal.x").unlink()
    assert vibeheal_configured(repo, cfg, {}).env_file == repo / ".env"
    (repo / ".env").unlink()
    assert vibeheal_configured(repo, cfg, {}).env_file == only_cfg


def test_same_dir_deduplicated(tmp_path):
    f = touch(tmp_path / ".env", ALL_VARS)
    assert vibeheal_configured(tmp_path, tmp_path, {}).env_file == f
    assert vibeheal_configured(tmp_path, None, {}).env_file == f


@pytest.mark.parametrize("cfg_name", [".harness.toml", "harness.toml", ".pr_review.toml", "pr_review.toml"])
@pytest.mark.parametrize("where", ["repo", "cfg"])
def test_reference_from_pr_review_config(dirs, cfg_name, where):
    repo, cfg = dirs
    base = repo if where == "repo" else cfg
    touch(
        base / cfg_name, '[[repo]]\nname="x"\nworking_dir="."\n[[repo.subdir]]\npath="a"\nenv_file="secrets/vh.env"\n'
    )
    target = touch(base / "secrets" / "vh.env")
    res = vibeheal_configured(repo, cfg, {})
    assert (res.configured, res.env_file) == (True, target)


def test_absolute_reference_and_missing_target(dirs, tmp_path):
    repo, cfg = dirs
    target = touch(tmp_path / "abs.env")
    touch(repo / ".harness.toml", f'[[repo.subdir]]\nenv_file="{target}"\n')
    assert vibeheal_configured(repo, cfg, {}).env_file == target
    target.unlink()
    assert not vibeheal_configured(repo, cfg, {}).configured


def test_direct_env_file_beats_reference(dirs):
    repo, cfg = dirs
    direct = touch(repo / ".env", ALL_VARS)
    touch(repo / ".harness.toml", '[[repo.subdir]]\nenv_file="x.env"\n')
    touch(repo / "x.env")
    assert vibeheal_configured(repo, cfg, {}).env_file == direct


@pytest.mark.parametrize("text", ["[[repo", "repo = 3", "[[repo]]\nsubdir = 1", "[[repo.subdir]]\nenv_file = 5"])
def test_malformed_or_odd_toml_is_no_reference(dirs, text):
    repo, cfg = dirs
    touch(repo / ".pr_review.toml", text)
    assert vibeheal_configured(repo, cfg, {}) == prework_inputs.VibeHealDetection(False, None)


def test_is_configured_predicate():
    assert _is_configured(Path("x"), {}) is True
    assert _is_configured(None, ENV_ALL) is True
    assert _is_configured(None, {}) is False
    assert _is_configured(None, {"SONARQUBE_URL": "u"}) is False


def test_bare_env_without_vibeheal_vars_not_configured(dirs):
    repo, cfg = dirs
    touch(repo / ".env", "DATABASE_URL=postgres://x\n")
    assert vibeheal_configured(repo, cfg, {}) == prework_inputs.VibeHealDetection(False, None)


def test_bare_env_with_partial_vars_not_configured(dirs):
    repo, cfg = dirs
    touch(repo / ".env", "SONARQUBE_URL=u\nSONARQUBE_TOKEN=\n")
    assert not vibeheal_configured(repo, cfg, {}).configured


def test_bare_env_with_export_and_quotes(dirs):
    repo, cfg = dirs
    f = touch(repo / ".env", "# c\nexport SONARQUBE_URL='u'\nSONARQUBE_TOKEN=\"t\"\nSONARQUBE_PROJECT_KEY=k\n")
    assert vibeheal_configured(repo, cfg, {}).env_file == f


def test_explicit_vibeheal_file_counts_even_if_empty(dirs):
    repo, cfg = dirs
    assert vibeheal_configured(repo, cfg, {}).configured is False
    touch(repo / ".env.vibeheal")
    assert vibeheal_configured(repo, cfg, {}).configured is True


def test_process_environment_alone_configures(dirs):
    repo, cfg = dirs
    assert vibeheal_configured(repo, cfg, ENV_ALL) == prework_inputs.VibeHealDetection(True, None)


def test_env_defaults_to_process_environment(dirs, monkeypatch):
    repo, cfg = dirs
    for k, v in ENV_ALL.items():
        monkeypatch.setenv(k, v)
    assert vibeheal_configured(repo, cfg).configured
