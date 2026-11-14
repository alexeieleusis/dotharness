from __future__ import annotations

from pathlib import Path

import pytest

from dotharness.sisyphus import config as config_mod
from dotharness.sisyphus.config import (
    DEFAULT_CONFIG_FILE,
    ConfigError,
    ProjectConfig,
    RefactorConfig,
    SisyphusConfig,
    load_config,
)

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
    assert cfg.limits.max_leaves_per_run == 32
    assert cfg.limits.max_run_minutes == 180
    assert not hasattr(cfg, "merge")
    assert cfg.project.repo_dir is None
    assert cfg.source.work_item is None
    assert cfg.agent.backend is None
    assert cfg.digest.path_template is None
    assert cfg.knowledge.sizing_file is None


def test_archive_dir_default(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, MIN)).archive.dir == "archive"
    assert load_config(write(tmp_path, MIN + "[archive]\n")).archive.dir == "archive"


@pytest.mark.parametrize("value", ["out/archive", "/abs/archive", "~/archive", "../a"])
def test_archive_dir_verbatim(tmp_path: Path, value: str) -> None:
    assert load_config(write(tmp_path, MIN + f'[archive]\ndir = "{value}"\n')).archive.dir == value


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
    assert not hasattr(cfg.source, "ci")
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


def test_source_ci_must_be_table(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(write(tmp_path, MIN + "[source]\nci = 1\n"))


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
        "archive",
        "refactor",
    ],
)
def test_unknown_key_in_each_section(tmp_path: Path, section: str) -> None:
    text = f"{MIN}[{section}]\nbogus = 1\n" if section != "project" else "[project]\nbogus = 1\n"
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write(tmp_path, text))


@pytest.mark.parametrize("other", ["linear", "github", "file"])
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


def test_file_kind(tmp_path: Path) -> None:
    text = MIN + '[source.work_item]\nkind = "file"\ndir = "~/.sisyphus/inbox"\n'
    wi = load_config(write(tmp_path, text)).source.work_item
    assert wi is not None
    assert wi.kind == "file"
    assert wi.dir == "~/.sisyphus/inbox"
    assert wi.assignee is None
    assert wi.pick_state is None
    assert wi.pick_label is None


def test_file_kind_producer_keys(tmp_path: Path) -> None:
    text = MIN + '[source.work_item]\nkind = "file"\ndir = "inbox"\nassignee = "a@b.c"\n'
    text += 'pick_state = "Todo"\npick_label = "sisyphus"\n'
    wi = load_config(write(tmp_path, text)).source.work_item
    assert wi is not None
    assert (wi.assignee, wi.pick_state, wi.pick_label) == ("a@b.c", "Todo", "sisyphus")


def test_file_kind_requires_dir(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dir"):
        load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "file"\n'))


@pytest.mark.parametrize("kind", ["linear", "github"])
def test_dir_rejected_for_other_kinds(tmp_path: Path, kind: str) -> None:
    with pytest.raises(ConfigError, match="dir"):
        load_config(write(tmp_path, MIN + f'[source.work_item]\nkind = "{kind}"\ndir = "x"\n'))


def test_dir_must_be_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="dir must be a string"):
        load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "file"\ndir = 1\n'))


def test_file_kind_no_default_label(tmp_path: Path) -> None:
    wi = load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "file"\ndir = "x"\n')).source.work_item
    assert wi is not None
    assert wi.pick_label is None


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
        ("[project]\n[archive]\ndir = 1\n", "dir"),
        ("[project]\n[refactor]\nenabled = 1\n", "enabled"),
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
    assert not hasattr(cfg.source, "ci")
    cfg = load_config(write(tmp_path, MIN + '[source.ci]\nkind = "gitlab"\n'))
    assert not hasattr(cfg.source, "ci")


def test_source_ci_does_not_change_config(tmp_path: Path) -> None:
    without = load_config(write(tmp_path, MIN, "a.toml"))
    with_ci = load_config(write(tmp_path, MIN + '[source.ci]\nkind = "github"\n', "b.toml"))
    assert with_ci.source == without.source
    assert not hasattr(with_ci.source, "ci")


def test_representative_config_with_source_ci_loads(tmp_path: Path) -> None:
    text = MIN + '[source.ci]\nkind = "github"\n[limits]\nmax_run_minutes = 5\n[merge]\nenabled = true\n'
    cfg = load_config(write(tmp_path, text))
    assert cfg.limits.max_run_minutes == 5
    assert not hasattr(cfg.source, "ci")


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


def test_remove_label_when_settled_defaults_on_for_github(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "github"\n'))
    assert cfg.source.work_item is not None
    assert cfg.source.work_item.remove_label_when_settled is True
    off = MIN + '[source.work_item]\nkind = "github"\nremove_label_when_settled = false\n'
    cfg = load_config(write(tmp_path, off))
    assert cfg.source.work_item is not None
    assert cfg.source.work_item.remove_label_when_settled is False


def test_remove_label_when_settled_rejected_for_linear(tmp_path: Path) -> None:
    text = MIN + '[source.work_item]\nkind = "linear"\nremove_label_when_settled = true\n'
    with pytest.raises(ConfigError, match=r"remove_label_when_settled is a github key.*linear.*mixed tracker"):
        load_config(write(tmp_path, text))
    cfg = load_config(write(tmp_path, MIN + '[source.work_item]\nkind = "linear"\n'))
    assert cfg.source.work_item is not None
    assert cfg.source.work_item.remove_label_when_settled is None


def test_remove_label_when_settled_ignored_for_file(tmp_path: Path) -> None:
    text = MIN + '[source.work_item]\nkind = "file"\ndir = "t"\nremove_label_when_settled = true\n'
    assert load_config(write(tmp_path, text)).source.work_item is not None


def test_remove_label_when_settled_must_be_boolean(tmp_path: Path) -> None:
    text = MIN + '[source.work_item]\nkind = "github"\nremove_label_when_settled = "yes"\n'
    with pytest.raises(ConfigError, match="remove_label_when_settled must be a boolean"):
        load_config(write(tmp_path, text))


def test_refactor_table_empty(tmp_path: Path) -> None:
    assert load_config(write(tmp_path, MIN + "[refactor]\n")).refactor == RefactorConfig()


@pytest.mark.parametrize("value", [True, False])
def test_refactor_enabled(tmp_path: Path, value: bool) -> None:
    cfg = load_config(write(tmp_path, MIN + f"[refactor]\nenabled = {str(value).lower()}\n"))
    assert cfg.refactor.enabled is value


def test_refactor_enabled_not_bool(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[refactor\] enabled must be a boolean"):
        load_config(write(tmp_path, MIN + '[refactor]\nenabled = "yes"\n'))


def test_refactor_unknown_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bogus"):
        load_config(write(tmp_path, MIN + "[refactor]\nbogus = 1\n"))
    with pytest.raises(ConfigError, match=r"unknown key 'bogus' in \[refactor\] \(allowed: enabled, pattern_set\)"):
        load_config(write(tmp_path, MIN + "[refactor]\nbogus = 1\n"))


def test_refactor_absent_defaults_enabled(tmp_path: Path) -> None:
    cfg = load_config(write(tmp_path, MIN))
    assert cfg.refactor.enabled is True
    assert cfg == SisyphusConfig(project=ProjectConfig())


@pytest.mark.parametrize("name", ["fastapi", "generic", "react"])
def test_refactor_pattern_set_accepted(tmp_path: Path, name: str) -> None:
    cfg = load_config(write(tmp_path, MIN + f'[refactor]\npattern_set = "{name}"\n'))
    assert cfg.refactor.pattern_set == name


def test_refactor_pattern_set_unknown(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, MIN + '[refactor]\npattern_set = "django"\n'))
    assert "[refactor] pattern_set must be one of fastapi, generic, react, got 'django'" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "FastAPI", " fastapi"])
def test_refactor_pattern_set_exact_match(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match=r"\[refactor\] pattern_set must be one of"):
        load_config(write(tmp_path, MIN + f'[refactor]\npattern_set = "{bad}"\n'))


def test_refactor_pattern_set_not_string(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[refactor\] pattern_set must be a string, got int"):
        load_config(write(tmp_path, MIN + "[refactor]\npattern_set = 3\n"))


@pytest.mark.parametrize("body", ["", "[refactor]\n", "[refactor]\nenabled = false\n"])
def test_refactor_pattern_set_absent_reads_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    def boom() -> tuple[str, ...]:
        pytest.fail("helper must not be called")

    monkeypatch.setattr(config_mod.stack, "pattern_set_names", boom)
    cfg = load_config(write(tmp_path, MIN + body))
    assert cfg.refactor.pattern_set is None


def test_refactor_pattern_set_list_from_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "pats"
    (root / "beta").mkdir(parents=True)
    (root / "alpha").mkdir()
    (root / "notes.md").write_text("x")

    def fake() -> tuple[str, ...]:
        return tuple(sorted(p.name for p in root.iterdir() if p.is_dir()))

    monkeypatch.setattr(config_mod.stack, "pattern_set_names", fake)
    cfg = load_config(write(tmp_path, MIN + '[refactor]\npattern_set = "alpha"\n'))
    assert cfg.refactor.pattern_set == "alpha"
    with pytest.raises(ConfigError, match="alpha, beta"):
        load_config(write(tmp_path, MIN + '[refactor]\npattern_set = "fastapi"\n'))


def test_refactor_pattern_set_missing_generic_is_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dotharness.paths.default_knowledge_dir", lambda: tmp_path)
    with pytest.raises(ConfigError, match=r"cannot validate \[refactor\] pattern_set: .*generic is missing"):
        load_config(write(tmp_path, MIN + '[refactor]\npattern_set = "fastapi"\n'))
