"""sonar-project.properties detection, command building, and file patching."""

import logging
import os
import re
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dotharness.vibe_heal.config import VibeHealConfig

logger = logging.getLogger(__name__)

PROPERTIES_FILENAME = "sonar-project.properties"

_KEY_RE = re.compile(r"^\s*sonar\.projectKey\s*=", re.IGNORECASE)
_NAME_RE = re.compile(r"^\s*sonar\.projectName\s*=", re.IGNORECASE)
_AUTH_PROPS_RE = re.compile(r"^\s*sonar\.(token|login|password)\s*=\s*.+", re.IGNORECASE)
_AUTH_REDACT_RE = re.compile(r"^\s*[#!]?\s*sonar\.(token|login|password)\s*=\s*.+", re.IGNORECASE)
_AUTH_ENV_VARS = ("SONAR_TOKEN", "SONAR_LOGIN")
_HOST_URL_ENV_VARS = ("SONAR_HOST_URL",)
_HOST_URL_RE = re.compile(r"^\s*sonar\.host\.url\s*=\s*.+", re.IGNORECASE)
_RECOVERY_HEADER_MARKER = "vibe-heal: temporary analysis project"


class SonarPropertiesHandler:
    """Detects sonar-project.properties, builds scanner commands, and patches the file for temp projects."""

    def __init__(self, project_dir: Path, config: "VibeHealConfig") -> None:
        self.project_dir = project_dir
        self.config = config
        self.properties_file = project_dir / PROPERTIES_FILENAME

    @property
    def exists(self) -> bool:
        return self.properties_file.is_file()

    def build_command(
        self,
        project_key: str,
        project_name: str,
        sources: list[Path] | None = None,
    ) -> list[str]:
        if not self.exists:
            return self._build_full_command(project_key, project_name, sources)
        command = ["sonar-scanner"]
        if not self._has_host_url_configured():
            command.append(f"-Dsonar.host.url={self.config.sonarqube_url}")
        if not self._has_auth_configured():
            if self.config.use_token_auth:
                command.append(f"-Dsonar.token={self.config.sonarqube_token}")
            else:
                command.append(f"-Dsonar.login={self.config.sonarqube_username}")
                command.append(f"-Dsonar.password={self.config.sonarqube_password}")
        return command

    def _build_full_command(
        self,
        project_key: str,
        project_name: str,
        sources: list[Path] | None = None,
    ) -> list[str]:
        command = [
            "sonar-scanner",
            f"-Dsonar.projectKey={project_key}",
            f"-Dsonar.projectName={project_name}",
            f"-Dsonar.host.url={self.config.sonarqube_url}",
        ]
        if self.config.use_token_auth:
            command.append(f"-Dsonar.token={self.config.sonarqube_token}")
        else:
            command.append(f"-Dsonar.login={self.config.sonarqube_username}")
            command.append(f"-Dsonar.password={self.config.sonarqube_password}")
        if sources:
            command.append(f"-Dsonar.sources={','.join(str(s) for s in sources)}")
        else:
            command.append("-Dsonar.sources=.")
        return command

    def _has_auth_configured(self) -> bool:
        for env_var in _AUTH_ENV_VARS:
            if os.environ.get(env_var):
                return True
        if self.exists:
            content = self.properties_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if _AUTH_PROPS_RE.match(line):
                    return True
        return False

    def _has_host_url_configured(self) -> bool:
        for env_var in _HOST_URL_ENV_VARS:
            if os.environ.get(env_var):
                return True
        if self.exists:
            content = self.properties_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                if _HOST_URL_RE.match(line):
                    return True
        return False

    @contextmanager
    def patched(self, project_key: str, project_name: str) -> Generator[None, None, None]:
        if not self.exists:
            yield
            return
        disk_content = self.properties_file.read_text(encoding="utf-8")
        original_content = _recover_true_original(disk_content)
        if original_content != disk_content:
            logger.warning(
                "%s was left mid-patch by an interrupted prior run; recovering the original before continuing.",
                self.properties_file,
            )
            self.properties_file.write_text(original_content, encoding="utf-8")

        existing_key = extract_property(original_content, "sonar.projectKey")
        if existing_key == project_key:
            yield
            return
        patched_content = _patch_content(original_content, project_key, project_name)
        self.properties_file.write_text(patched_content, encoding="utf-8")
        try:
            yield
        finally:
            try:
                self.properties_file.write_text(original_content, encoding="utf-8")
            except OSError:
                logger.exception(
                    "Failed to restore %s. Original content (auth properties redacted):\n%s",
                    self.properties_file,
                    _redact_auth_properties(original_content),
                )


def _recover_true_original(content: str) -> str:
    """Recover the true pre-patch content when a prior run was killed before its own
    restore in `patched()` ran, leaving stale recovery header(s) plus a temporary
    projectKey/projectName on disk. The topmost commented projectKey/projectName pair
    is always the real original, since it was captured before any prior corruption.
    """
    lines = content.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if _RECOVERY_HEADER_MARKER in line), None)
    if start is None:
        return content

    end = start
    while end < len(lines) and (
        _RECOVERY_HEADER_MARKER in lines[end]
        or lines[end].lstrip().startswith("# restore the lines below")
        or _match_commented_property(lines[end], _KEY_RE)
        or _match_commented_property(lines[end], _NAME_RE)
        or _KEY_RE.match(lines[end])
        or _NAME_RE.match(lines[end])
    ):
        end += 1

    cruft = lines[start:end]
    recovered: list[str] = []
    for pattern in (_KEY_RE, _NAME_RE):
        commented = next((ln for ln in cruft if _match_commented_property(ln, pattern)), None)
        if commented is not None:
            recovered.append(commented.lstrip().removeprefix("#").lstrip())

    return "".join(lines[:start] + recovered + lines[end:])


def _match_commented_property(line: str, pattern: re.Pattern[str]) -> bool:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return False
    return bool(pattern.match(stripped.removeprefix("#").lstrip()))


def extract_property(content: str, key: str) -> str | None:
    """Extract the value of a `key=value` property from sonar-project.properties content."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)", re.IGNORECASE)
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            continue
        m = pattern.match(line)
        if m:
            return m.group(1).strip()
    return None


def _redact_auth_properties(content: str) -> str:
    """Redact auth-related sonar properties before logging file contents."""

    redacted_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if _AUTH_REDACT_RE.match(line):
            key, sep, remainder = line.partition("=")
            line_ending = ""
            if remainder.endswith("\r\n"):
                line_ending = "\r\n"
            elif remainder.endswith("\n"):
                line_ending = "\n"
            elif remainder.endswith("\r"):
                line_ending = "\r"
            redacted_lines.append(f"{key}{sep}<redacted>{line_ending}")
        else:
            redacted_lines.append(line)
    return "".join(redacted_lines)


def _find_key_name_indices(
    lines: list[str],
) -> tuple[list[int], list[int], str | None, str | None]:
    """Return (key_indices, name_indices, orig_key_line, orig_name_line) from a properties file.

    Collects ALL non-commented occurrences so duplicates don't survive patching
    and override the temporary values (Java properties files use last-wins semantics).
    """
    key_indices: list[int] = []
    name_indices: list[int] = []
    orig_key_line: str | None = None
    orig_name_line: str | None = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if _KEY_RE.match(line):
            key_indices.append(i)
            orig_key_line = line.rstrip("\n").rstrip("\r")
        elif _NAME_RE.match(line):
            name_indices.append(i)
            orig_name_line = line.rstrip("\n").rstrip("\r")
    return key_indices, name_indices, orig_key_line, orig_name_line


def _build_recovery_block(
    orig_key_line: str | None, orig_name_line: str | None, new_key: str, new_name: str
) -> list[str]:
    """Build the recovery comment block and new key/name lines."""
    recovery: list[str] = [
        "# vibe-heal: temporary analysis project. If this process was interrupted,\n",
        "# restore the lines below (remove the '#' prefix):\n",
    ]
    if orig_key_line is not None:
        recovery.append(f"# {orig_key_line.lstrip()}\n")
    if orig_name_line is not None:
        recovery.append(f"# {orig_name_line.lstrip()}\n")
    recovery.append(f"sonar.projectKey={new_key}\n")
    recovery.append(f"sonar.projectName={new_name}\n")
    return recovery


def _patch_content(content: str, new_key: str, new_name: str) -> str:
    lines = content.splitlines(keepends=True)
    key_indices, name_indices, orig_key_line, orig_name_line = _find_key_name_indices(lines)

    recovery = _build_recovery_block(orig_key_line, orig_name_line, new_key, new_name)

    all_indices = key_indices + name_indices
    if not all_indices:
        if not content.endswith("\n"):
            content += "\n"
        # Include the recovery header so an interrupted run can be undone by _recover_true_original.
        return content + "".join(recovery)

    first_idx = min(all_indices)
    skip = set(all_indices)
    result: list[str] = []
    for i, line in enumerate(lines):
        if i == first_idx:
            result.extend(recovery)
        if i not in skip:
            result.append(line)
    return "".join(result)
