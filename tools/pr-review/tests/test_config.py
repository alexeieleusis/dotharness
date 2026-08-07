import pytest

from harness.config import ConfigError, PreCommand, load_config


def test_load_minimal(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.repo.name == "acme/frontend"
    assert cfg.harness.backend == "opencode"


def test_defaults_applied(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.harness.backend_timeout_seconds == 900
    assert cfg.harness.gh_token_cmd == "echo test-token"  # noqa: S105
    assert cfg.vibe_heal.enabled is False


def test_tilde_expanded_in_working_dir(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "~/dev/repo"
""")
    cfg = load_config(p)
    assert not str(cfg.repo.working_dir).startswith("~")


def test_tilde_expanded_in_knowledge_dir(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
knowledge_dir = "~/.harness/knowledge"
[repo]
name = "a/b"
working_dir = "/tmp"
""")
    cfg = load_config(p)
    assert not str(cfg.harness.knowledge_dir).startswith("~")


def test_path_prepend_order_preserved(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[harness.path_prepend]
java = "/java/bin"
node = "/node/bin"
[repo]
name = "a/b"
working_dir = "/tmp"
""")
    cfg = load_config(p)
    assert cfg.harness.path_prepend == ["/java/bin", "/node/bin"]


def test_invalid_backend_raises(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
backend = "gpt4"
[repo]
name = "a/b"
working_dir = "/tmp"
""")
    with pytest.raises(ConfigError, match="backend"):
        load_config(p)


def test_opencode_dir_inside_working_dir_parses(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text(f"""
[harness]
[repo]
name = "a/b"
working_dir = "{tmp_path}"
opencode_dir = "{tmp_path}/plugins/foo"
""")
    cfg = load_config(p)
    assert cfg.repo.opencode_dir == tmp_path / "plugins/foo"


def test_opencode_dir_defaults_to_none(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.repo.opencode_dir is None


def test_opencode_dir_outside_working_dir_raises(tmp_path):
    p = tmp_path / ".harness.toml"
    other_dir = tmp_path.parent / "elsewhere"
    p.write_text(f"""
[harness]
[repo]
name = "a/b"
working_dir = "{tmp_path}"
opencode_dir = "{other_dir}"
""")
    with pytest.raises(ConfigError, match="opencode_dir"):
        load_config(p)


def test_missing_repo_name_raises(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
working_dir = "/tmp"
""")
    with pytest.raises(ConfigError, match=r"repo\.name"):
        load_config(p)


def test_subdir_missing_path_raises(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[[repo.subdir]]
pre_commands = ["npm ci"]
""")
    with pytest.raises(ConfigError, match=r"repo\.subdir.*path"):
        load_config(p)


def test_subdirs_parsed(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[[repo.subdir]]
path = "."
pre_commands = ["pnpm install"]
coverage = true
timeout = 300
""")
    cfg = load_config(p)
    assert len(cfg.repo.subdirs) == 1
    assert cfg.repo.subdirs[0].coverage is True
    assert cfg.repo.subdirs[0].pre_commands == [PreCommand(cmd="pnpm install")]


def test_subdir_pre_commands_table_form(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[[repo.subdir]]
path = "."
pre_commands = [
  { cmd = "poetry install", critical = true },
  { cmd = "pnpm ci" },
  "npm ci",
]
""")
    cfg = load_config(p)
    assert cfg.repo.subdirs[0].pre_commands == [
        PreCommand(cmd="poetry install", critical=True),
        PreCommand(cmd="pnpm ci", critical=False),
        PreCommand(cmd="npm ci", critical=False),
    ]


def test_repo_slug(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.repo_slug == "acme-frontend"


def test_focused_review_defaults(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.focused_review.enabled is False
    assert str(cfg.focused_review.vibe_types_repo).endswith(".harness/vendor/vibe-types")
    assert not str(cfg.focused_review.vibe_types_repo).startswith("~")


def test_focused_review_parsed(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[focused_review]
enabled = true
vibe_types_repo = "~/custom/vibe-types"
""")
    cfg = load_config(p)
    assert cfg.focused_review.enabled is True
    assert not str(cfg.focused_review.vibe_types_repo).startswith("~")
    assert str(cfg.focused_review.vibe_types_repo).endswith("custom/vibe-types")


def test_address_comments_defaults(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.address_comments.require_reaction_for_focused_review is False


def test_address_comments_parsed(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[address_comments]
require_reaction_for_focused_review = true
""")
    cfg = load_config(p)
    assert cfg.address_comments.require_reaction_for_focused_review is True
