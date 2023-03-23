from __future__ import annotations


import click

from eth_definitions.builtin_defs import check_builtin
from eth_definitions.download import download
from eth_definitions.sign import sign_definitions


@click.group()
def cli() -> None:
    """Script for handling Ethereum definitions (networks and tokens)."""


cli.add_command(download)
cli.add_command(check_builtin)
cli.add_command(sign_definitions)


if __name__ == "__main__":
    cli()
