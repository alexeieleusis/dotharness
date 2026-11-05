"""Tests for SonarPropertiesHandler."""

from pathlib import Path

import pytest

from dotharness.vibe_heal.config import VibeHealConfig
from dotharness.vibe_heal.sonarqube.properties_handler import (
    SonarPropertiesHandler,
    _patch_content,
    _recover_true_original,
    _redact_auth_properties,
)


@pytest.fixture
def config() -> VibeHealConfig:
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_token="test-token",  # noqa: S106
        sonarqube_project_key="my-project",
    )


@pytest.fixture
def basic_auth_config() -> VibeHealConfig:
    return VibeHealConfig(
        sonarqube_url="https://sonar.test.com",
        sonarqube_username="user",
        sonarqube_password="pass",  # noqa: S106
        sonarqube_project_key="my-project",
    )


class TestExists:
    def test_false_when_file_absent(self, tmp_path: Path, config: VibeHealConfig) -> None:
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler.exists is False

    def test_true_when_file_present(self, tmp_path: Path, config: VibeHealConfig) -> None:
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=x\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler.exists is True


class TestBuildCommandNoFile:
    def test_includes_all_d_flags_token_auth(self, tmp_path: Path, config: VibeHealConfig) -> None:
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("test-key", "Test Project")
        assert "-Dsonar.projectKey=test-key" in cmd
        assert "-Dsonar.projectName=Test Project" in cmd
        assert "-Dsonar.host.url=https://sonar.test.com" in cmd
        assert "-Dsonar.token=test-token" in cmd
        assert "-Dsonar.sources=." in cmd

    def test_default_sources_dot(self, tmp_path: Path, config: VibeHealConfig) -> None:
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("key", "Name", sources=None)
        assert "-Dsonar.sources=." in cmd

    def test_explicit_sources(self, tmp_path: Path, config: VibeHealConfig) -> None:
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("key", "Name", sources=[Path("src/a.py"), Path("src/b.py")])
        assert "-Dsonar.sources=src/a.py,src/b.py" in cmd

    def test_basic_auth(self, tmp_path: Path, basic_auth_config: VibeHealConfig) -> None:
        handler = SonarPropertiesHandler(tmp_path, basic_auth_config)
        cmd = handler.build_command("key", "Name")
        assert "-Dsonar.login=user" in cmd
        assert "-Dsonar.password=pass" in cmd
        assert not any("sonar.token" in arg for arg in cmd)


class TestHasAuthConfigured:
    def test_false_when_nothing_set(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is False

    def test_true_via_sonar_token_env(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_TOKEN", "secret")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is True

    def test_false_via_sonarqube_token_env(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        monkeypatch.setenv("SONARQUBE_TOKEN", "secret")
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is False
        cmd = handler.build_command("key", "Name")
        assert any(arg.startswith("-Dsonar.token=") for arg in cmd)

    def test_true_via_sonar_login_env(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_LOGIN", "user")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is True

    def test_true_via_token_in_file(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        (tmp_path / "sonar-project.properties").write_text("sonar.token=file-token\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is True

    def test_true_via_login_in_file(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        (tmp_path / "sonar-project.properties").write_text("sonar.login=user\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is True

    def test_commented_token_line_not_counted(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        (tmp_path / "sonar-project.properties").write_text("# sonar.token=commented\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        assert handler._has_auth_configured() is False


class TestBuildCommandWithFile:
    def test_minimal_command_when_auth_in_file(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\nsonar.token=file-token\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert cmd == ["sonar-scanner", "-Dsonar.host.url=https://sonar.test.com"]

    def test_minimal_command_when_auth_in_env(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_TOKEN", "env-token")
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert cmd == ["sonar-scanner", "-Dsonar.host.url=https://sonar.test.com"]

    def test_injects_token_fallback_when_no_auth(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert cmd == ["sonar-scanner", "-Dsonar.host.url=https://sonar.test.com", "-Dsonar.token=test-token"]

    def test_injects_basic_auth_fallback_when_no_auth(
        self, tmp_path: Path, basic_auth_config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for v in ("SONAR_TOKEN", "SONARQUBE_TOKEN", "SONAR_LOGIN"):
            monkeypatch.delenv(v, raising=False)
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, basic_auth_config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert cmd == [
            "sonar-scanner",
            "-Dsonar.host.url=https://sonar.test.com",
            "-Dsonar.login=user",
            "-Dsonar.password=pass",
        ]

    def test_sources_not_injected_when_file_present(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_TOKEN", "tok")
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("key", "Name", sources=[Path("src/a.py")])
        assert not any("sonar.sources" in arg for arg in cmd)

    def test_host_url_not_injected_when_configured_in_file(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_TOKEN", "tok")
        (tmp_path / "sonar-project.properties").write_text(
            "sonar.projectKey=orig\nsonar.host.url=https://other.test.com\n"
        )
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert not any("sonar.host.url" in arg for arg in cmd)

    def test_host_url_not_injected_when_configured_in_env(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_TOKEN", "tok")
        monkeypatch.setenv("SONAR_HOST_URL", "https://env.test.com")
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert not any("sonar.host.url" in arg for arg in cmd)

    def test_host_url_injected_when_not_configured(
        self, tmp_path: Path, config: VibeHealConfig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SONAR_TOKEN", "tok")
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=orig\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        cmd = handler.build_command("temp-key", "Temp Name")
        assert "-Dsonar.host.url=https://sonar.test.com" in cmd


class TestPatchContent:
    def test_replaces_key_and_name_with_recovery_block(self) -> None:
        original = "sonar.projectKey=my-project\nsonar.projectName=My Project\nsonar.sources=.\n"
        result = _patch_content(original, "temp-key-123", "my-project review user main")
        assert "sonar.projectKey=temp-key-123" in result
        assert "sonar.projectName=my-project review user main" in result
        assert "# sonar.projectKey=my-project" in result
        assert "# sonar.projectName=My Project" in result
        assert "vibe-heal: temporary analysis project" in result
        assert "sonar.sources=." in result  # unrelated line preserved

    def test_key_only_in_file_appends_new_name(self) -> None:
        original = "sonar.projectKey=my-project\nsonar.sources=.\n"
        result = _patch_content(original, "temp-key-123", "my-project review user main")
        assert "sonar.projectKey=temp-key-123" in result
        assert "sonar.projectName=my-project review user main" in result
        assert "# sonar.projectKey=my-project" in result
        assert "sonar.sources=." in result

    def test_neither_key_nor_name_appended_at_end(self) -> None:
        original = "sonar.sources=.\nsonar.host.url=https://sonar.test.com\n"
        result = _patch_content(original, "temp-key-123", "my-project review user main")
        assert result.endswith("sonar.projectKey=temp-key-123\nsonar.projectName=my-project review user main\n")
        assert "sonar.sources=." in result

    def test_commented_key_line_not_treated_as_property(self) -> None:
        original = "# sonar.projectKey=commented\nsonar.sources=.\n"
        result = _patch_content(original, "temp-key-123", "my-project review user main")
        # No real key found, so values appended at end
        assert result.endswith("sonar.projectKey=temp-key-123\nsonar.projectName=my-project review user main\n")

    def test_appends_newline_when_content_has_no_trailing_newline(self) -> None:
        original = "sonar.sources=."
        result = _patch_content(original, "temp-key-123", "Temp Name")
        assert "sonar.sources=.\n" in result
        assert "sonar.sources=.sonar.projectKey" not in result
        assert result.endswith("sonar.projectKey=temp-key-123\nsonar.projectName=Temp Name\n")

    def test_duplicate_key_name_lines_all_removed(self) -> None:
        original = (
            "sonar.projectKey=first-project\n"
            "sonar.projectName=First Project\n"
            "sonar.sources=.\n"
            "sonar.projectKey=second-project\n"
            "sonar.projectName=Second Project\n"
        )
        result = _patch_content(original, "temp-key-123", "Temp Name")
        assert "sonar.projectKey=temp-key-123" in result
        assert "sonar.projectName=Temp Name" in result
        # Duplicate lines should be removed; first-project is gone entirely
        assert "first-project" not in result
        # second-project appears only as a recovery comment, not as an active key line
        assert "\nsonar.projectKey=second-project\n" not in result
        # The recovery comment should reference the last (active) values
        assert "# sonar.projectKey=second-project" in result
        assert "# sonar.projectName=Second Project" in result
        assert "sonar.sources=." in result


class TestRecoverTrueOriginal:
    def test_returns_content_unchanged_when_no_recovery_header(self) -> None:
        clean = "sonar.projectKey=fe\nsonar.projectName=Oscilar Frontend\nsonar.sources=src\n"
        assert _recover_true_original(clean) == clean

    def test_recovers_original_behind_single_dangling_patch(self) -> None:
        dangling = (
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# sonar.projectKey=fe\n"
            "# sonar.projectName=Oscilar Frontend\n"
            "sonar.projectKey=temp-key\n"
            "sonar.projectName=Temp Name\n"
            "sonar.sources=src\n"
        )
        assert _recover_true_original(dangling) == (
            "sonar.projectKey=fe\nsonar.projectName=Oscilar Frontend\nsonar.sources=src\n"
        )

    def test_recovers_original_behind_repeatedly_stacked_dangling_patches(self) -> None:
        """Regression test for the exact corruption observed in production: three
        stacked recovery headers (three interrupted runs in a row) with only the
        topmost commented pair holding the real original."""
        stacked = (
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# sonar.projectKey=fe\n"
            "# sonar.projectName=Oscilar Frontend\n"
            "sonar.projectKey=fe_alexei_oscilar_com_dependabot_npm_and_yarn_npm_minor_patch_dad4541c73_260720-0544\n"
            "sonar.projectName=fe review alexei dependabot-npm_and_yarn-npm-minor-patch-dad4541c73\n"
            "sonar.sources=src\n"
            "sonar.tests=tests\n"
        )
        assert _recover_true_original(stacked) == (
            "sonar.projectKey=fe\nsonar.projectName=Oscilar Frontend\nsonar.sources=src\nsonar.tests=tests\n"
        )

    def test_drops_active_pair_entirely_when_no_original_ever_existed(self) -> None:
        """If the properties file never had an active projectKey/projectName before
        the first patch, there's nothing to recover -- the whole dangling block
        (header + stale active pair) should simply be dropped."""
        dangling = (
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "sonar.projectKey=temp-key\n"
            "sonar.projectName=Temp Name\n"
            "sonar.sources=src\n"
        )
        assert _recover_true_original(dangling) == "sonar.sources=src\n"

    def test_preserves_unrelated_comment_immediately_after_active_pair(self) -> None:
        """A plain comment that happens to follow the active projectKey/projectName
        lines is not part of the recovery block and must survive recovery."""
        dangling = (
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# sonar.projectKey=fe\n"
            "# sonar.projectName=Oscilar Frontend\n"
            "sonar.projectKey=temp-key\n"
            "sonar.projectName=Temp Name\n"
            "# Important: keep sources scoped to src only\n"
            "sonar.sources=src\n"
        )
        assert _recover_true_original(dangling) == (
            "sonar.projectKey=fe\n"
            "sonar.projectName=Oscilar Frontend\n"
            "# Important: keep sources scoped to src only\n"
            "sonar.sources=src\n"
        )


class TestPatched:
    def test_patches_file_during_with_block(self, tmp_path: Path, config: VibeHealConfig) -> None:
        props = tmp_path / "sonar-project.properties"
        props.write_text("sonar.projectKey=original\nsonar.sources=.\n")
        handler = SonarPropertiesHandler(tmp_path, config)
        with handler.patched("temp-key", "Temp Name"):
            content = props.read_text()
            assert "sonar.projectKey=temp-key" in content
            assert "# sonar.projectKey=original" in content

    def test_recovers_pristine_original_after_crash_in_append_branch(
        self, tmp_path: Path, config: VibeHealConfig
    ) -> None:
        original = "sonar.sources=src\n"
        props = tmp_path / "sonar-project.properties"
        props.write_text(original)
        handler = SonarPropertiesHandler(tmp_path, config)
        # Simulate SIGKILL: enter the patch and never run the restore.
        cm = handler.patched("old-temp-key", "Old Temp Name")
        cm.__enter__()
        assert "sonar.projectKey=old-temp-key" in props.read_text()
        # A fresh run must recover the pristine original and restore it on exit.
        with handler.patched("new-temp-key", "New Temp Name"):
            content = props.read_text()
            assert "new-temp-key" in content
            assert "old-temp-key" not in content
        assert props.read_text() == original

    def test_restores_file_after_normal_exit(self, tmp_path: Path, config: VibeHealConfig) -> None:
        original = "sonar.projectKey=original\nsonar.sources=.\n"
        props = tmp_path / "sonar-project.properties"
        props.write_text(original)
        handler = SonarPropertiesHandler(tmp_path, config)
        with handler.patched("temp-key", "Temp Name"):
            pass
        assert props.read_text() == original

    def test_restores_file_after_exception(self, tmp_path: Path, config: VibeHealConfig) -> None:
        original = "sonar.projectKey=original\nsonar.sources=.\n"
        props = tmp_path / "sonar-project.properties"
        props.write_text(original)
        handler = SonarPropertiesHandler(tmp_path, config)
        with pytest.raises(ValueError), handler.patched("temp-key", "Temp Name"):
            raise ValueError("simulated failure")  # noqa: TRY003
        assert props.read_text() == original

    def test_noop_when_file_absent(self, tmp_path: Path, config: VibeHealConfig) -> None:
        handler = SonarPropertiesHandler(tmp_path, config)
        with handler.patched("temp-key", "Temp Name"):
            pass  # must not raise

    def test_noop_when_key_already_matches(self, tmp_path: Path, config: VibeHealConfig) -> None:
        original = "sonar.projectKey=same-key\n"
        props = tmp_path / "sonar-project.properties"
        props.write_text(original)
        handler = SonarPropertiesHandler(tmp_path, config)
        with handler.patched("same-key", "Same Name"):
            assert props.read_text() == original  # file untouched

    def test_recovers_true_original_when_file_left_mid_patch(self, tmp_path: Path, config: VibeHealConfig) -> None:
        """A prior run was killed before its own restore ran, leaving a dangling
        recovery header + a stale temp key/name on disk. The next patched()
        call must recover the real original rather than treat that stale
        state as ground truth to restore back to later."""
        dangling = (
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# sonar.projectKey=fe\n"
            "# sonar.projectName=Oscilar Frontend\n"
            "sonar.projectKey=stale-other-pr-key\n"
            "sonar.projectName=stale-other-pr-name\n"
            "sonar.sources=src\n"
        )
        props = tmp_path / "sonar-project.properties"
        props.write_text(dangling)
        handler = SonarPropertiesHandler(tmp_path, config)
        with handler.patched("temp-key", "Temp Name"):
            content = props.read_text()
            assert "sonar.projectKey=temp-key" in content
            assert "# sonar.projectKey=fe" in content  # recovered original, not the stale one
            assert "stale-other-pr-key" not in content

        # Restores to the recovered original, not the dangling stale state.
        assert props.read_text() == "sonar.projectKey=fe\nsonar.projectName=Oscilar Frontend\nsonar.sources=src\n"

    def test_recovers_immediately_even_when_new_key_matches_recovered_key(
        self, tmp_path: Path, config: VibeHealConfig
    ) -> None:
        """If the recovered original key happens to equal the key being requested
        (e.g. a baseline scan against the real project key), patched() short-circuits
        without patching -- but the dangling stale state must still be healed on
        disk immediately, since nothing else will write the recovered content."""
        dangling = (
            "# vibe-heal: temporary analysis project. If this process was interrupted,\n"
            "# restore the lines below (remove the '#' prefix):\n"
            "# sonar.projectKey=fe\n"
            "# sonar.projectName=Oscilar Frontend\n"
            "sonar.projectKey=stale-other-pr-key\n"
            "sonar.projectName=stale-other-pr-name\n"
        )
        props = tmp_path / "sonar-project.properties"
        props.write_text(dangling)
        handler = SonarPropertiesHandler(tmp_path, config)
        with handler.patched("fe", "fe"):
            assert props.read_text() == "sonar.projectKey=fe\nsonar.projectName=Oscilar Frontend\n"


class TestRedactAuthProperties:
    def test_redacts_uncommented_token(self) -> None:
        result = _redact_auth_properties("sonar.token=my-secret\n")
        assert "my-secret" not in result
        assert "<redacted>" in result

    def test_redacts_commented_token_with_hash(self) -> None:
        result = _redact_auth_properties("# sonar.token=my-secret\n")
        assert "my-secret" not in result
        assert "<redacted>" in result

    def test_redacts_commented_token_with_exclamation(self) -> None:
        result = _redact_auth_properties("! sonar.login=user123\n")
        assert "user123" not in result
        assert "<redacted>" in result

    def test_redacts_password_in_file(self) -> None:
        result = _redact_auth_properties("  sonar.password=p@ssw0rd\n")
        assert "p@ssw0rd" not in result
        assert "<redacted>" in result

    def test_preserves_non_auth_lines(self) -> None:
        original = "sonar.projectKey=my-project\nsonar.token=secret123\nsonar.sources=.\n# sonar.password=oldpass\n"
        result = _redact_auth_properties(original)
        assert "sonar.projectKey=my-project\n" in result
        assert "sonar.sources=.\n" in result
        assert "secret123" not in result
        assert "oldpass" not in result
        assert result.count("<redacted>") == 2
