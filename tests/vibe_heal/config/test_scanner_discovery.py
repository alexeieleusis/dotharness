"""Tests for scanner_discovery."""

from pathlib import Path

import pytest

from dotharness.vibe_heal.config.scanner_discovery import resolve_scanner_auth, resolve_scanner_host_url


@pytest.fixture(autouse=True)
def _clear_scanner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("SONAR_TOKEN", "SONAR_LOGIN", "SONAR_PASSWORD", "SONAR_SCANNER_HOME", "SONAR_HOST_URL"):
        monkeypatch.delenv(var, raising=False)


class TestResolveScannerAuth:
    """Tests for resolve_scanner_auth."""

    def test_returns_none_when_nothing_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env vars, no properties files, no scanner install: nothing found."""
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert resolve_scanner_auth(tmp_path) is None

    def test_env_token_takes_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SONAR_TOKEN env var is used when set."""
        monkeypatch.setenv("SONAR_TOKEN", "env-token")

        assert resolve_scanner_auth(tmp_path) == {"token": "env-token"}

    def test_env_login_and_password(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SONAR_LOGIN + SONAR_PASSWORD env vars are used together."""
        monkeypatch.setenv("SONAR_LOGIN", "env-user")
        monkeypatch.setenv("SONAR_PASSWORD", "env-pass")

        assert resolve_scanner_auth(tmp_path) == {
            "login": "env-user",
            "password": "env-pass",
        }

    def test_env_login_without_password_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A lone SONAR_LOGIN without SONAR_PASSWORD isn't usable."""
        monkeypatch.setenv("SONAR_LOGIN", "env-user")
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert resolve_scanner_auth(tmp_path) is None

    def test_project_properties_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sonar.token in the project's sonar-project.properties is used."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        (tmp_path / "sonar-project.properties").write_text("sonar.projectKey=my-project\nsonar.token=proj-token\n")

        assert resolve_scanner_auth(tmp_path) == {"token": "proj-token"}

    def test_project_properties_login_and_password(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sonar.login + sonar.password in sonar-project.properties are used together."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        (tmp_path / "sonar-project.properties").write_text(
            "sonar.login=proj-user\nsonar.password=proj-pass\n",
        )

        assert resolve_scanner_auth(tmp_path) == {
            "login": "proj-user",
            "password": "proj-pass",
        }

    def test_commented_properties_are_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Commented-out sonar.token lines don't count as configured."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        (tmp_path / "sonar-project.properties").write_text("#sonar.token=proj-token\n")

        assert resolve_scanner_auth(tmp_path) is None

    def test_global_properties_via_scanner_home_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SONAR_SCANNER_HOME/conf/sonar-scanner.properties is used when the env var is set."""
        scanner_home = tmp_path / "scanner"
        conf_dir = scanner_home / "conf"
        conf_dir.mkdir(parents=True)
        (conf_dir / "sonar-scanner.properties").write_text("sonar.token=global-token\n")
        monkeypatch.setenv("SONAR_SCANNER_HOME", str(scanner_home))

        assert resolve_scanner_auth(tmp_path / "project") == {"token": "global-token"}

    def test_global_properties_via_resolved_binary_flat_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """conf/sonar-scanner.properties next to a resolved `which sonar-scanner` binary is used."""
        install_root = tmp_path / "sonar-scanner-install"
        (install_root / "bin").mkdir(parents=True)
        binary = install_root / "bin" / "sonar-scanner"
        binary.write_text("#!/bin/sh\n")
        conf_dir = install_root / "conf"
        conf_dir.mkdir()
        (conf_dir / "sonar-scanner.properties").write_text("sonar.token=flat-layout-token\n")
        monkeypatch.setattr("shutil.which", lambda name: str(binary))

        assert resolve_scanner_auth(tmp_path / "project") == {"token": "flat-layout-token"}

    def test_global_properties_via_resolved_binary_libexec_layout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """libexec/conf/sonar-scanner.properties (e.g. Homebrew's layout) is also checked."""
        install_root = tmp_path / "sonar-scanner-install"
        (install_root / "bin").mkdir(parents=True)
        binary = install_root / "bin" / "sonar-scanner"
        binary.write_text("#!/bin/sh\n")
        conf_dir = install_root / "libexec" / "conf"
        conf_dir.mkdir(parents=True)
        (conf_dir / "sonar-scanner.properties").write_text("sonar.token=libexec-layout-token\n")
        monkeypatch.setattr("shutil.which", lambda name: str(binary))

        assert resolve_scanner_auth(tmp_path / "project") == {"token": "libexec-layout-token"}

    def test_project_properties_take_priority_over_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project-level sonar-project.properties wins over the global scanner config."""
        scanner_home = tmp_path / "scanner"
        conf_dir = scanner_home / "conf"
        conf_dir.mkdir(parents=True)
        (conf_dir / "sonar-scanner.properties").write_text("sonar.token=global-token\n")
        monkeypatch.setenv("SONAR_SCANNER_HOME", str(scanner_home))

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "sonar-project.properties").write_text("sonar.token=proj-token\n")

        assert resolve_scanner_auth(project_dir) == {"token": "proj-token"}


class TestResolveScannerHostUrl:
    """Tests for resolve_scanner_host_url."""

    def test_returns_none_when_nothing_configured(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env var, no properties files, no scanner install: nothing found."""
        monkeypatch.setattr("shutil.which", lambda name: None)

        assert resolve_scanner_host_url(tmp_path) is None

    def test_env_var_takes_priority(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """SONAR_HOST_URL env var is used when set."""
        monkeypatch.setenv("SONAR_HOST_URL", "https://env.example.com")

        assert resolve_scanner_host_url(tmp_path) == "https://env.example.com"

    def test_project_properties_host_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sonar.host.url in the project's sonar-project.properties is used."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        (tmp_path / "sonar-project.properties").write_text("sonar.host.url=https://proj.example.com\n")

        assert resolve_scanner_host_url(tmp_path) == "https://proj.example.com"

    def test_global_properties_host_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sonar.host.url in the scanner installation's global sonar-scanner.properties is used."""
        scanner_home = tmp_path / "scanner"
        conf_dir = scanner_home / "conf"
        conf_dir.mkdir(parents=True)
        (conf_dir / "sonar-scanner.properties").write_text("sonar.host.url=https://global.example.com\n")
        monkeypatch.setenv("SONAR_SCANNER_HOME", str(scanner_home))

        assert resolve_scanner_host_url(tmp_path / "project") == "https://global.example.com"

    def test_project_properties_take_priority_over_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A project-level sonar-project.properties wins over the global scanner config."""
        scanner_home = tmp_path / "scanner"
        conf_dir = scanner_home / "conf"
        conf_dir.mkdir(parents=True)
        (conf_dir / "sonar-scanner.properties").write_text("sonar.host.url=https://global.example.com\n")
        monkeypatch.setenv("SONAR_SCANNER_HOME", str(scanner_home))

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "sonar-project.properties").write_text("sonar.host.url=https://proj.example.com\n")

        assert resolve_scanner_host_url(project_dir) == "https://proj.example.com"
