"""Console script stubs for the four tools; replaced as each tool is migrated."""

import typer

STUB_MESSAGE = "{name}: not yet migrated, see docs/migration-ledger.md"


def _make_stub(name: str) -> typer.Typer:
    app = typer.Typer(name=name, add_completion=False)

    @app.callback(invoke_without_command=True)
    def main() -> None:
        typer.echo(STUB_MESSAGE.format(name=name))

    return app


vibe_heal = _make_stub("vibe_heal")
pr_review = _make_stub("pr_review")
spec_prism_flow = _make_stub("spec_prism_flow")
sisyphus = _make_stub("sisyphus")
