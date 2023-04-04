import logging

import click

from pandablocks_ioc.ioc import create_softioc

__all__ = ["cli"]


@click.group(invoke_without_command=True)
@click.option(
    "--log-level",
    default="INFO",
    type=click.Choice(
        ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"], case_sensitive=False
    ),
)
@click.version_option()
@click.pass_context
def cli(ctx, log_level: str):
    """PandaBlocks client library command line interface."""

    level = getattr(logging, log_level.upper(), None)
    logging.basicConfig(format="%(levelname)s:%(message)s", level=level)

    # if no command is supplied, print the help message
    if ctx.invoked_subcommand is None:
        click.echo(cli.get_help(ctx))


@cli.command()
@click.argument("host")
@click.argument("prefix")
@click.argument("screens")
def softioc(host: str, prefix: str, screens: str):
    """
    Create a soft IOC, using "prefix" for the namespace of the records.
    """
    create_softioc(host=host, record_prefix=prefix, screens=screens)


# test with: python -m pandablocks_ioc
if __name__ == "__main__":
    cli()
