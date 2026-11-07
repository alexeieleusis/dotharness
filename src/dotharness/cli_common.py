from typing import NoReturn

import typer


def fail(message: str) -> NoReturn:
    """Typer equivalent of ``click.ClickException``: ``Error: <message>`` on stderr, exit code 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(1)
