"""Discovers SonarQube auth and host URL from the same sources `sonar-scanner` itself would use.

Machines that already run `sonar-scanner` directly often have credentials and a server
URL configured outside of vibe_heal's own `.env.vibeheal`/`.env` (scanner-convention env
vars, a project's `sonar-project.properties`, or the scanner installation's global
`sonar-scanner.properties`). This lets `VibeHealConfig` reuse that configuration
instead of requiring it to be duplicated.
"""

import os
import shutil
from pathlib import Path

from dotharness.vibe_heal.sonarqube.properties_handler import PROPERTIES_FILENAME, extract_property


def resolve_scanner_auth(project_dir: Path) -> dict[str, str] | None:
    """Resolve SonarQube auth the way `sonar-scanner` would resolve it.

    Checks, in order: SONAR_TOKEN / SONAR_LOGIN+SONAR_PASSWORD env vars, the
    project's sonar-project.properties, then the scanner installation's global
    sonar-scanner.properties. Returns `{"token": ...}` or `{"login": ..., "password": ...}`,
    or None if no auth was found anywhere.
    """
    token = os.environ.get("SONAR_TOKEN")
    if token:
        return {"token": token}
    login = os.environ.get("SONAR_LOGIN")
    password = os.environ.get("SONAR_PASSWORD")
    if login and password:
        return {"login": login, "password": password}

    project_properties = project_dir / PROPERTIES_FILENAME
    if project_properties.is_file():
        auth = _extract_auth(project_properties.read_text(encoding="utf-8"))
        if auth is not None:
            return auth

    global_properties = _find_global_scanner_properties()
    if global_properties is not None:
        return _extract_auth(global_properties.read_text(encoding="utf-8"))
    return None


def resolve_scanner_host_url(project_dir: Path) -> str | None:
    """Resolve the SonarQube host URL the way `sonar-scanner` would resolve it.

    Checks, in order: SONAR_HOST_URL env var, the project's sonar-project.properties,
    then the scanner installation's global sonar-scanner.properties. Returns the URL,
    or None if none was found anywhere.
    """
    host_url = os.environ.get("SONAR_HOST_URL")
    if host_url:
        return host_url

    project_properties = project_dir / PROPERTIES_FILENAME
    if project_properties.is_file():
        url = extract_property(project_properties.read_text(encoding="utf-8"), "sonar.host.url")
        if url:
            return url

    global_properties = _find_global_scanner_properties()
    if global_properties is not None:
        return extract_property(global_properties.read_text(encoding="utf-8"), "sonar.host.url")
    return None


def _find_global_scanner_properties() -> Path | None:
    """Locate the scanner installation's global sonar-scanner.properties, if any.

    Install layouts vary by machine and packaging (official SonarSource archives
    put `conf/` next to `bin/`; Homebrew's wrapper sets SONAR_SCANNER_HOME to a
    `libexec/` directory), so this derives the location structurally rather than
    assuming a fixed path.
    """
    scanner_home = os.environ.get("SONAR_SCANNER_HOME")
    if scanner_home:
        candidate = Path(scanner_home) / "conf" / "sonar-scanner.properties"
        if candidate.is_file():
            return candidate

    scanner_path = shutil.which("sonar-scanner")
    if scanner_path is None:
        return None
    install_root = Path(scanner_path).resolve().parent.parent
    for relative in ("conf/sonar-scanner.properties", "libexec/conf/sonar-scanner.properties"):
        candidate = install_root / relative
        if candidate.is_file():
            return candidate
    return None


def _extract_auth(content: str) -> dict[str, str] | None:
    token = extract_property(content, "sonar.token")
    if token:
        return {"token": token}
    login = extract_property(content, "sonar.login")
    password = extract_property(content, "sonar.password")
    if login and password:
        return {"login": login, "password": password}
    return None
