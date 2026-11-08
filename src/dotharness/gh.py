"""GitHub CLI knowledge kept out of the generic ``subprocess_runner``: the ``gh`` argv and the env policy."""

import os

GH = "gh"


def gh_argv(*args: str) -> list[str]:
    """The one place that names the GitHub CLI: argv for ``gh <args>``, run through ``run`` by the caller's adapter."""
    return [GH, *args]


def build_subprocess_env(
    path_prepend: list[str], env_vars: dict[str, str], gh_token: str | None = None
) -> dict[str, str]:
    env = os.environ.copy()
    if path_prepend:
        env["PATH"] = ":".join(path_prepend) + ":" + env.get("PATH", "")
    env.update(env_vars)
    if gh_token:
        env["GITHUB_TOKEN"] = gh_token
    return env
