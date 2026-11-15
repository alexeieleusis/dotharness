"""Private to the reference code host: the one place that names the GitHub CLI. Nothing else imports this module."""

GH = "gh"


def gh_argv(*args: str) -> list[str]:
    """The one place that names the GitHub CLI: argv for ``gh <args>``, run through ``run`` by the reference host."""
    return [GH, *args]
