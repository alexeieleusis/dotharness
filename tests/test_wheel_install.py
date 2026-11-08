"""Build the wheel, install it into a clean venv outside the repo, and exercise entry points and packaged data."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = list(tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]["scripts"])
# One packaged data file per data-shipping tool (vibe_heal ships none).
DATA_FILES = [
    "knowledge/pr-review/review-summary.md",
    "knowledge/sisyphus/sizing-rules.md",
    "knowledge/spec-prism-flow/lifecycle-cheat-sheet.md",
    "skills/asd-ste100/SKILL.md",
]

PASSTHROUGH_ENV = (
    "PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME",
    "UV_CACHE_DIR", "UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "UV_OFFLINE", "UV_PYTHON_INSTALL_DIR",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
)  # fmt: skip
_NETWORK_ERRORS = ("Failed to fetch", "error sending request", "dns error", "Could not connect", "offline mode")

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


def _run(args: list[str | Path], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=300, check=False)


def _skip_if_offline(result: subprocess.CompletedProcess[str]) -> None:
    # shortcut: matches uv's stderr wording, update if uv changes its network error text.
    if result.returncode != 0 and any(m in result.stderr for m in _NETWORK_ERRORS):
        pytest.skip(f"package index unreachable: {result.stderr.strip().splitlines()[-1]}")


@pytest.fixture(scope="module")
def venv(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    root = tmp_path_factory.mktemp("wheel")
    # Allowlist: index, proxy, cert and cache settings only; no credentials from .env* leak into the children (NG6).
    env = {k: os.environ[k] for k in PASSTHROUGH_ENV if k in os.environ}
    uv = str(shutil.which("uv"))
    built = _run([uv, "build", "--wheel", "--out-dir", root / "dist"], REPO_ROOT, env)
    _skip_if_offline(built)
    assert built.returncode == 0, built.stderr
    wheels = list((root / "dist").glob("dotharness-*.whl"))
    assert len(wheels) == 1
    py = root / "venv" / "bin" / "python"
    made = _run([uv, "venv", "--python", sys.executable, root / "venv"], root, env)
    assert made.returncode == 0, made.stderr
    installed = _run([uv, "pip", "install", "--python", py, wheels[0]], root, env)
    _skip_if_offline(installed)
    assert installed.returncode == 0, installed.stderr
    return root / "venv", env


@pytest.mark.parametrize("script", SCRIPTS)
def test_console_script_help(venv: tuple[Path, dict[str, str]], script: str) -> None:
    root, env = venv
    result = _run([root / "bin" / script, "--help"], root.parent, env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


def test_packaged_data_resolves_from_install(venv: tuple[Path, dict[str, str]]) -> None:
    root, env = venv
    code = (
        "import sys; from importlib import resources; "
        "base = resources.files('dotharness'); "
        "assert 'site-packages' in str(base), base; "
        "bad = [f for f in sys.argv[1:] if not base.joinpath(f).is_file() or not base.joinpath(f).read_text()]; "
        "sys.exit('missing: ' + ', '.join(bad) if bad else 0)"
    )
    result = _run([root / "bin" / "python", "-I", "-c", code, *DATA_FILES], root.parent, env)
    assert result.returncode == 0, result.stderr
