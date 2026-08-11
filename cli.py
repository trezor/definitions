from __future__ import annotations

import click

from definitions.common import (
    DEFINITIONS_FORMAT_VERSION,
    load_definitions_data,
    store_definitions_data,
)
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


@cli.command()
@click.argument("version", required=False, type=int)
def bump_version(version: int | None) -> None:
    """Bump the definitions blob version stored in metadata.

    Without an argument, increments the current version by one; an explicit
    VERSION argument sets it directly. Use this on any backward-incompatible
    change (e.g. ERC-7730 serialization changes, signing-scheme changes).

    The version is not part of the signed Merkle root, so no re-signing is
    needed after bumping it alone. If the bump accompanies a serialization
    change, the Merkle root changes and the definitions must be re-signed.
    """
    metadata, definitions_data = load_definitions_data()
    current = metadata.get("version", DEFINITIONS_FORMAT_VERSION)
    new = version if version is not None else current + 1
    if new <= current:
        raise click.ClickException(
            f"New version ({new}) must be greater than the current one ({current})."
        )
    metadata["version"] = new
    store_definitions_data(metadata, definitions_data)
    click.echo(f"Definitions blob version bumped: {current} -> {new}")


if __name__ == "__main__":
    cli()
