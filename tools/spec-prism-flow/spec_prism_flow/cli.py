import click


@click.group()
def cli():
    pass  # Click group container; subcommands are registered via @cli.command()


@cli.group("plan")
def cmd_plan():
    """Requirements refinement: brief -> phase corpus (Feature A)."""


@cli.group("build")
def cmd_build():
    """Phase execution: phase corpus -> merged code (Feature B)."""


if __name__ == "__main__":
    cli()
