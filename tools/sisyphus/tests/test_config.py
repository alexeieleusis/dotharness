from __future__ import annotations

from pathlib import Path

import pytest

from sisyphus.config import DEFAULT_CONFIG_FILE, ConfigError, SisyphusConfig, load_config

MIN = "[project]\n"


def write(tmp_path: Path, text: str, name: str = DEFAULT_CONFIG_FILE) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_default_name() -> None:
    assert DEFAULT_CONFIG_FILE == ".sisyphus.toml"


def test_minimal_defaults(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MIN))
    assert isinstance(cfg, SisyphusConfig)
    assert cfg.limits.max_tickets_per_run == 1
    assert cfg.limits.max_leaves_per_run == 4
    assert cfg.limits.max_run_minutes == 180
    assert not hasattr(cfg, "merge")
    assert cfg.project.repo_dir is None
    assert cfg.source.work_item is None
    assert cfg.agent.backend is None
    assert cfg.digest.path_template is None
    assert cfg.knowledge.sizing_file is None


def test_str_path_accepted(tmp_path: Path) -> None:
    assert load_config(str(write(tmp_path, MIN))).project.repo_dir is None


def test_github_pick_label_default(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "github"\n'))
    assert cfg.source.work_item is not None
    assert cfg.source.work_item.pick_label == "sisyphus"


def test_github_pick_label_override_and_linear_none(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "github"\npick_label = "bot"\n'))
    assert cfg.source.work_item is not None
    assert cfg.source.work_item.pick_label == "bot"
    cfg = load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "linear"\npick_state = "Todo"\n'))
    assert cfg.source.work_item is not None
    assert cfg.source.work_item.pick_label is None
    assert cfg.source.work_item.pick_state == "Todo"


def test_full_config(tmp_path: Path) -> None:
    text = """
[project]
repo_dir = "r"
workspace_dir = "w"
brief_path_template = "b/{id}.md"
[source.work_item]
kind = "linear"
instruction = "do"
assignee = "me"
[source.ci]
kind = "github"
max_fix_attempts = 2
[source.host]
token_cmd = "gh auth token"  # noqa
[agent]
backend = "opencode"
[limits]
max_run_minutes = 10
[merge]
enabled = true
require_all_files_viewed = true
max_changed_lines = 50
[digest]
path_template = "d/{date}.md"
[knowledge]
sizing_file = "s"
conventions_path = "c"
"""
    cfg = load_config(write(tmp_path, text))
    assert cfg.project.brief_path_template == "b/{id}.md"
    assert cfg.project.base_branch is None
    assert cfg.source.ci is not None
    assert cfg.source.host is not None
    assert cfg.source.host.token_cmd == "gh auth token"  # noqa: S105
    assert cfg.agent.backend == "opencode"
    assert cfg.limits.max_run_minutes == 10
    assert not hasattr(cfg, "merge")
    assert cfg.knowledge.conventions_path == "c"


def test_missing_project(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[project\]"):
        load_config(write(tmp_path, '[agent]\nbackend = "claude"\n'))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path)


def test_bad_work_item_kind(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="kind"):
        load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "jira"\n'))


def test_bad_ci_kind(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="kind"):
        load_config(write(tmp_path, MIN + '[source.ci]\nkind = "gitlab"\n'))


def test_bad_backend(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="backend"):
        load_config(write(tmp_path, MIN + '[agent]\nbackend = "codex"\n'))


def test_unknown_top_level(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mystery"):
        load_config(write(tmp_path, MIN + "mystery = 1\n"))


@pytest.mark.parametrize(
    "section",
    [
        "project",
        "source",
        "source.work_item",
        "source.host",
        "agent",
        "limits",
        "digest",
        "knowledge",
    ],
)
def test_unknown_key_in_each_section(tmp_path: Path, section: str) -> None:
    text = f"{MIN}[{section}]\nbogus = 1\n" if section != "project" else "[project]\nbogus = 1\n"
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize("other", ["linear", "github"])
def test_two_trackers(tmp_path: Path, other: str) -> None:
    text = MIN + f'[source.work_item]\nkind = "linear"\n[source.{other}]\nteam = "x"\n'
    with pytest.raises(ConfigError, match="two trackers"):
        load_config(write(tmp_path, text))


def test_tracker_table_alone_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"source\.linear"):
        load_config(write(tmp_path, MIN + "[source.linear]\n"))


def test_unknown_source_subtable(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="jira"):
        load_config(write(tmp_path, MIN + "[source.jira]\n"))


def test_cross_kind_keys(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="pick_state"):
        load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "github"\npick_state = "Todo"\n'))
    with pytest.raises(ConfigError, match="pick_label"):
        load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "linear"\npick_label = "x"\n'))


def test_relative_paths_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg_dir = tmp_path / "elsewhere"
    cfg_dir.mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    text = '[project]\nrepo_dir = "repo"\nworkspace_dir = "../ws"\nbrief_path_template = "b/{id}.md"\n'
    text += '[digest]\npath_template = "d.md"\n[knowledge]\nsizing_file = "s.toml"\nconventions_path = "c.md"\n'
    p = write(cfg_dir, text)
    monkeypatch.chdir(cwd)
    cfg = load_config(p)
    assert cfg.project.repo_dir == "repo"
    assert cfg.project.workspace_dir == "../ws"
    assert cfg.project.brief_path_template == "b/{id}.md"
    assert cfg.digest.path_template == "d.md"
    assert cfg.knowledge.sizing_file == "s.toml"
    assert cfg.knowledge.conventions_path == "c.md"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[project]\nrepo_dir = 3\n", "repo_dir"),
        ("[project]\nbase_branch = 3\n", "base_branch"),
        ('[project]\n[limits]\nmax_run_minutes = "x"\n', "max_run_minutes"),
        ("[project]\n[limits]\nmax_run_minutes = 1.5\n", "max_run_minutes"),
        ("[project]\n[limits]\nmax_run_minutes = true\n", "max_run_minutes"),
        ("[project]\nsource = 1\n", "source"),
        ("project = 1\n", "project"),
    ],
)
def test_wrong_types(tmp_path: Path, text: str, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize("key", ["max_run_minutes", "max_tickets_per_run", "max_leaves_per_run"])
@pytest.mark.parametrize("value", [0, -1])
def test_limits_positive(tmp_path: Path, key: str, value: int) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(write(tmp_path, MIN + f"[limits]\n{key} = {value}\n"))


def test_harness_owned_keys_pass_through(tmp_path: Path) -> None:
    text = MIN + "[merge]\nmax_changed_lines = 0\nfuture_key = 1\n[source.ci]\nmax_fix_attempts = -2\nfuture = 1\n"
    cfg = load_config(write(tmp_path, text))
    assert not hasattr(cfg, "merge")
    assert cfg.source.ci is not None


def test_removed_fix_attempts_limit_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key 'max_fix_attempts_per_pr'"):
        load_config(write(tmp_path, MIN + "[limits]\nmax_fix_attempts_per_pr = 3\n"))


def test_merge_table_ignored_and_limits_still_parse(tmp_path: Path) -> None:
    text = MIN + '[merge]\nenabled = "yes"\nfuture = [1]\n'
    text += "[limits]\nmax_run_minutes = 5\nmax_tickets_per_run = 2\nmax_leaves_per_run = 3\n"
    cfg = load_config(write(tmp_path, text))
    assert not hasattr(cfg, "merge")
    assert (cfg.limits.max_run_minutes, cfg.limits.max_tickets_per_run, cfg.limits.max_leaves_per_run) == (5, 2, 3)


def test_invalid_toml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[project\n"))


@pytest.mark.parametrize("table", ["build", "plan", "review", "vibe_heal"])
def test_other_tool_keys_rejected(tmp_path: Path, table: str) -> None:
    with pytest.raises(ConfigError, match=table):
        load_config(write(tmp_path, MIN + f"[{table}]\ncommands = []\n"))
    with pytest.raises(ConfigError, match="gh_token_cmd"):
        load_config(write(tmp_path, 'gh_token_cmd = "x"\n' + MIN))
