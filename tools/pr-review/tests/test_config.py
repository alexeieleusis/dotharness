import os
import re

import pytest

from harness.config import ConfigError, PreCommand, load_config
from harness.lock import working_dir_lock_key
from harness.runners.common import build_subprocess_env


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


def test_tilde_expanded_in_review_knowledge_file(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
review_knowledge_file = "~/.harness/review-guide.md"
[repo]
name = "a/b"
working_dir = "/tmp"
""")
    cfg = load_config(p)
    assert cfg.harness.review_knowledge_file is not None
    assert not str(cfg.harness.review_knowledge_file).startswith("~")


def test_review_knowledge_file_defaults_to_none(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.harness.review_knowledge_file is None


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


def test_missing_working_dir_raises(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
""")
    with pytest.raises(ConfigError, match=r"repo\.working_dir"):
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


def test_pre_command_missing_cmd_raises(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[[repo.subdir]]
path = "."
pre_commands = [
  { critical = true },
]
""")
    with pytest.raises(ConfigError, match="cmd"):
        load_config(p)


def test_repo_slug(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.repo_slug == "acme-frontend"


def test_lock_key_delegates_to_working_dir_lock_key(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.lock_key == working_dir_lock_key(cfg.repo_slug, cfg.repo.working_dir)


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
    assert cfg.address_comments.enabled is True
    assert cfg.address_comments.trusted_commenters == "*"


def test_address_comments_disabled(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[address_comments]
enabled = false
""")
    cfg = load_config(p)
    assert cfg.address_comments.enabled is False


def test_min_reanalysis_interval_hours_default(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.vibe_heal.min_reanalysis_interval_hours == 24.0


def test_min_reanalysis_interval_hours_parsed(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[vibe_heal]
min_reanalysis_interval_hours = 6
""")
    cfg = load_config(p)
    assert cfg.vibe_heal.min_reanalysis_interval_hours == 6


def test_address_comments_parsed(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[address_comments]
trusted_commenters = ["alice", "bob"]
""")
    cfg = load_config(p)
    assert cfg.address_comments.trusted_commenters == ["alice", "bob"]


def test_regret_review_defaults(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.regret_review.enabled is False
    assert cfg.regret_review.max_diff_lines == 50
    assert cfg.regret_review.authors == "*"
    assert cfg.regret_review.regret_review_timeout == 300


def test_regret_review_parsed(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
[repo]
name = "a/b"
working_dir = "/tmp"

[regret_review]
enabled = true
max_diff_lines = 120
authors = ["alice", "bob"]
regret_review_timeout = 900
""")
    cfg = load_config(p)
    assert cfg.regret_review.enabled is True
    assert cfg.regret_review.max_diff_lines == 120
    assert cfg.regret_review.authors == ["alice", "bob"]
    assert cfg.regret_review.regret_review_timeout == 900


def test_malformed_toml_raises_config_error(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text("""
[harness]
backend = "opencode"
[repo
name = "a/b"
""")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(p)


_NO_NAME_TOML = """
[harness]
[repo]
working_dir = "{wd}"
{extra}
"""


def _write_no_name(tmp_path, wd=None, extra=""):
    p = tmp_path / ".harness.toml"
    p.write_text(_NO_NAME_TOML.format(wd=wd or tmp_path, extra=extra))
    return p


def test_missing_repo_name_still_raises_by_default(tmp_path):
    with pytest.raises(ConfigError, match=r"repo\.name is required"):
        load_config(_write_no_name(tmp_path))


def test_require_repo_name_false_allows_missing_name(tmp_path):
    cfg = load_config(_write_no_name(tmp_path), require_repo_name=False)
    assert cfg.repo.name == ""
    assert cfg.repo.name_provided is False


def test_working_dir_still_required_when_name_optional(tmp_path):
    p = tmp_path / ".harness.toml"
    p.write_text('[harness]\n[repo]\nname = "a/b"\n')
    with pytest.raises(ConfigError, match=r"repo\.working_dir is required"):
        load_config(p, require_repo_name=False)


def test_repo_slug_uses_name_when_present(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.repo.name_provided is True
    assert cfg.repo_slug == "acme-frontend"


def test_repo_slug_fallback_matches_state_regex_and_is_stable(tmp_path):
    wd = tmp_path / "my weird repo!"
    wd.mkdir()
    cfg = load_config(_write_no_name(tmp_path, wd=wd), require_repo_name=False)
    slug = cfg.repo_slug
    assert re.fullmatch(r"[a-zA-Z0-9._-]+", slug)
    assert re.fullmatch(r"my-weird-repo-[0-9a-f]{8}", slug)
    assert slug == cfg.repo_slug
    assert slug == load_config(_write_no_name(tmp_path, wd=wd), require_repo_name=False).repo_slug


def test_repo_slug_fallback_differs_per_path(tmp_path):
    a = tmp_path / "a" / "repo"
    b = tmp_path / "b" / "repo"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    slug_a = load_config(_write_no_name(tmp_path, wd=a), require_repo_name=False).repo_slug
    slug_b = load_config(_write_no_name(tmp_path, wd=b), require_repo_name=False).repo_slug
    assert slug_a != slug_b


def test_local_review_section_parsed(tmp_path):
    p = _write_no_name(tmp_path, extra="")
    p.write_text(p.read_text() + '\n[local_review]\nbase = "main"\noutput_dir = "~/reviews"\n')
    cfg = load_config(p, require_repo_name=False)
    assert cfg.local_review.base == "main"
    assert cfg.local_review.output_dir == "~/reviews"


def test_local_review_absent_yields_empty_config(minimal_toml):
    cfg = load_config(minimal_toml)
    assert cfg.local_review.base is None
    assert cfg.local_review.output_dir is None


def test_build_subprocess_env_without_token_omits_github_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    env = build_subprocess_env(["/x/bin"], {"FOO": "bar"}, None)
    assert "GITHUB_TOKEN" not in env
    assert env["FOO"] == "bar"
    assert env["PATH"].startswith("/x/bin:")
    assert "GITHUB_TOKEN" not in build_subprocess_env([], {})
    assert os.environ.get("GITHUB_TOKEN") is None


@pytest.mark.parametrize("bad", ["..", ".", "a b", "a;b"])
def test_invalid_repo_name_rejected(tmp_path, bad):
    p = tmp_path / ".harness.toml"
    p.write_text(f'[harness]\n[repo]\nname = "{bad}"\nworking_dir = "{tmp_path}"\n')
    with pytest.raises(ConfigError, match=r"Invalid repo\.name"):
        load_config(p)
    with pytest.raises(ConfigError, match=r"Invalid repo\.name"):
        load_config(p, require_repo_name=False)
