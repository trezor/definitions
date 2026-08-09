from __future__ import annotations

import click

from definitions.common import load_definitions_data
from definitions.download import download
from definitions.ethereum.builtin_defs import check_builtin
from definitions.generate import generate_definitions
from definitions.serialize import get_merkle_root
from definitions.sign import sign_definitions


@click.group()
def cli() -> None:
    """Script for handling coin/network/token definitions (Ethereum, Solana, ...)."""


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


@cli.command()
def computed_merkle_root():
    """Recompute the Merkle root from the definitions data.

    Unlike current-merkle-root, this does not trust the value stored in
    metadata but derives it from the definitions themselves. A warning is
    emitted if the two disagree (the stored value is stale or tampered)."""
    metadata, definitions_data = load_definitions_data()
    computed = get_merkle_root(definitions_data, metadata["unix_timestamp"])
    stored = metadata.get("merkle_root")
    if stored is not None and stored != computed:
        click.echo(
            "WARNING: computed Merkle root does not match the one stored in "
            "definitions-latest.json:\n"
            f"  computed: {computed}\n"
            f"  stored:   {stored}",
            err=True,
        )
    print(computed)


if __name__ == "__main__":
    cli()
