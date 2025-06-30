#!/usr/bin/env python3
from __future__ import annotations

import logging

import click
from cryptography.exceptions import InvalidSignature
from trezorlib.merkle_tree import MerkleTree

from .common import (
    get_git_commit_hash,
    load_definitions_data,
    setup_logging,
    store_definitions_data,
)
from .generate import serialize_with_progress
from .crypto import verify_signature
LOG = logging.getLogger(__name__)


@click.command(name="sign")
@click.argument("signature", required=True)
@click.option(
    "-V",
    "--verify",
    is_flag=True,
    help="Recompute merkle root and verify against metadata.",
)
@click.option("-v", "--verbose", is_flag=True, help="Display more info.")
def sign_definitions(
    signature: str,
    verify: bool,
    verbose: bool,
) -> None:
    """Verify signature and update metadata.

    Gets the current merkle root from metadata, verifies the signature,
    and if valid, writes it into metadata.

    If --verify is specified, also recomputes the merkle root and verifies
    it matches the one in metadata.
    """
    setup_logging(verbose)

    # load prepared definitions
    metadata, definitions_data = load_definitions_data()
    loaded_merkle_root = metadata["merkle_root"]

    if verify:
        # Recompute merkle root
        timestamp = metadata["unix_timestamp"]
        serializations = serialize_with_progress(definitions_data, timestamp)
        mt = MerkleTree(serializations.keys())
        computed_root = mt.get_root_hash().hex()

        if loaded_merkle_root != computed_root:
            raise click.ClickException(
                f"Computed merkle root ({computed_root}) does not match "
                f"the one in metadata ({loaded_merkle_root})."
            )

    root_hash = bytes.fromhex(loaded_merkle_root)

    # Convert signature to bytes
    signature_bytes = bytes.fromhex(signature)
    if len(signature_bytes) != 65:
        raise click.ClickException(
            "Provided `--signature` value is not valid. " "It should be 65 bytes long."
        )

    # Verify signature
    try:
        verify_signature(signature_bytes, root_hash)
    except InvalidSignature:
        raise click.ClickException(
            "Provided signature is not valid for current "
            f"Merkle tree root hash ({loaded_merkle_root})."
        )

    # Update metadata with new signature
    metadata["signature"] = signature_bytes.hex()
    metadata["commit_hash"] = get_git_commit_hash()
    store_definitions_data(metadata, definitions_data)
