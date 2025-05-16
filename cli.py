from __future__ import annotations


import click

from eth_definitions.builtin_defs import check_builtin
from eth_definitions.download import download
from eth_definitions.generate import generate_definitions
from eth_definitions.sign import sign_definitions

from eth_definitions.common import load_definitions_data


@click.group()
def cli() -> None:
    """Script for handling Ethereum definitions (networks and tokens)."""


cli.add_command(download)
cli.add_command(check_builtin)
cli.add_command(generate_definitions)
cli.add_command(sign_definitions)


@cli.command()
def current_merkle_root():
    """Print out the Merkle root stored in the definitions.

    Used in the shell script instead of having to get jq."""
    metadata, _ = load_definitions_data()
    print(metadata["merkle_root"])


if __name__ == "__main__":
    cli()
