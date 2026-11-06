"""Console script stubs for the four tools; replaced as each tool is migrated."""

import typer

from dotharness.pr_review.cli import cli as pr_review
from dotharness.vibe_heal.cli import app as vibe_heal

STUB_MESSAGE = "{name}: not yet migrated, see docs/migration-ledger.md"


def _make_stub(name: str) -> typer.Typer:
    app = typer.Typer(name=name, add_completion=False)

    @app.callback(invoke_without_command=True)
    def main() -> None:
        typer.echo(STUB_MESSAGE.format(name=name))

    return app


spec_prism_flow = _make_stub("spec_prism_flow")
sisyphus = _make_stub("sisyphus")

__all__ = ["pr_review", "sisyphus", "spec_prism_flow", "vibe_heal"]
