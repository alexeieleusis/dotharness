"""Async pr-review adapter over ``dotharness.ai_backend.launch``.

``Backend`` keeps pr-review's public contract (constructor, ``run`` returning a ``subprocess.CompletedProcess`` with
bytes output, ``subprocess.TimeoutExpired`` on timeout) while every launch, kill, retry and guard goes through the
shared layer. Result mapping (see ``docs/design/shared-async-layer.md``, "I-3 adapter contract"):

* timeout (``RunResult.timed_out``) raises ``subprocess.TimeoutExpired``;
* a non-zero exit and a missing executable (``not_found``, 127) are *returned* as a ``CompletedProcess`` with that
  return code; ``CalledProcessError`` is never raised (callers check ``returncode``);
* ``ValueError``, ``TypeError``, ``OpencodePluginError``, ``BackendCwdDivergedError`` and ``RepoIdentityError`` from the
  shared layer propagate unchanged.

Neither argv, env nor backend output is logged here.
"""

import functools
import logging
import subprocess
from pathlib import Path

from dotharness.ai_backend import BACKEND_NAMES, ClaudeOptions, OpencodeOptions, launch
from dotharness.opencode_guards import BackendCwdDivergedError, OpencodePluginError
from dotharness.repo_guard import RepoIdentityError

__all__ = ["Backend", "BackendCwdDivergedError", "OpencodePluginError", "RepoIdentityError", "find_harness_checkout"]

logger = logging.getLogger(__name__)


@functools.cache
def find_harness_checkout() -> Path | None:
    """Root of the dotharness source checkout this module runs from, or None when installed (wheel/site-packages).

    Pure path inspection, no git: a parent of this file qualifies only if it has a ``.git`` entry and this module lives
    under its ``src/dotharness/pr_review``. A site-packages install inside some unrelated repo therefore yields None
    instead of pinning that repo (the shared ``snapshot_harness_repo`` would otherwise run git from its own module dir).
    """
    here = Path(__file__).resolve().parent
    for parent in here.parents:
        if (parent / ".git").exists() and here == parent / "src" / "dotharness" / "pr_review":
            return parent
    return None


class Backend:
    def __init__(
        self,
        backend: str,
        timeout: int,
        path_prepend: list[str],
        env_vars: dict[str, str],
        max_retries: int = 1,
        *,
        expected_repo_name: str | None = None,
        harness_repo_root: Path | None = None,
    ):
        if backend not in BACKEND_NAMES:
            raise ValueError(f"Unknown backend: {backend}")  # noqa: TRY003
        self.backend_name = backend
        self.timeout = timeout
        self.path_prepend = path_prepend
        self.env_vars = env_vars
        self.max_retries = max_retries
        self.expected_repo_name = expected_repo_name
        # Explicit root wins; otherwise the source checkout (None when installed, which skips the harness-HEAD guard).
        self.harness_repo_root = harness_repo_root if harness_repo_root is not None else find_harness_checkout()

    async def run(
        self, instructions: str, cwd: str, opencode_dir: str | None = None, context: str | None = None
    ) -> subprocess.CompletedProcess:
        # Flag mapping per the I-2 "Contract for I-3, I-4 and I-5". The guards are opt-in in the shared layer, so they
        # are passed explicitly; max_retries is passed explicitly too (adapter default 1, shared default 0).
        if self.backend_name == "opencode":
            options: ClaudeOptions | OpencodeOptions = OpencodeOptions(
                standalone=True, auto=True, opencode_dir=opencode_dir, monitor_cwd=True, check_plugins=True
            )
        else:
            options = ClaudeOptions(skip_permissions=True, disable_slash_commands=True)
        if self.harness_repo_root is None:
            logger.debug("no dotharness checkout found (installed package); harness-HEAD guard skipped")
        result = await launch(
            self.backend_name,
            instructions,
            cwd=cwd,
            timeout=self.timeout,
            env=self.env_vars,
            path_prepend=self.path_prepend,
            context=context,
            max_retries=self.max_retries,
            expected_repo_name=self.expected_repo_name,
            harness_repo_root=self.harness_repo_root,
            backend_options=options,
        )
        if result.timed_out:
            raise subprocess.TimeoutExpired(
                self.backend_name, self.timeout, output=result.stdout.encode(), stderr=result.stderr.encode()
            )
        return subprocess.CompletedProcess(
            [self.backend_name], result.returncode, result.stdout.encode(), result.stderr.encode()
        )
