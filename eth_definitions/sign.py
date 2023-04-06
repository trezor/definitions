#!/usr/bin/env python3
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import click
import ed25519  # type: ignore

from trezorlib import cosi, definitions

from .common import (
    GENERATED_DEFINITIONS_DIR,
    Network,
    Token,
    serialize_definitions,
    load_definitions_data,
    setup_logging,
    store_definitions_data,
)
from .definitions_dev_sign import get_dev_public_key, sign_with_dev_keys

LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from trezorlib.merkle_tree import MerkleTree


# ====== definitions tools ======


def _write_path(path: Path, data: bytes, exists_ok: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not exists_ok:
            LOG.error("File %s already exists, not overwriting", path)
        else:
            LOG.info("Skipping existing file %s", path)
        return

    LOG.info("Writing %s", path)
    path.write_bytes(data)


def _generate_token_def(token: Token, serialized: bytes, base_path: Path) -> None:
    address = token["address"][2:].lower()
    file = base_path / "chain-id" / str(token["chain_id"]) / f"token-{address}.dat"
    _write_path(file, serialized)


def _generate_network_def(network: Network, serialized: bytes, base_path: Path) -> None:
    # create path for networks identified by chain and slip44 ids
    network_file = base_path / "chain-id" / str(network["chain_id"]) / "network.dat"
    slip44_file = base_path / "slip44" / str(network["slip44"]) / "network.dat"
    # save network definition
    _write_path(network_file, serialized)
    _write_path(slip44_file, serialized, exists_ok=True)


def _make_proof(
    serialized: bytes,
    tree: MerkleTree,
    signature: bytes,
) -> bytes:
    proof = tree.get_proof(serialized)
    proof_encoded = definitions.ProofFormat.build(proof)
    return proof_encoded + signature


def _combine_public_key(sigmask: int) -> bytes:
    selected_keys = [
        k
        for i, k in enumerate(definitions.DEFINITIONS_PUBLIC_KEYS)
        if sigmask & (1 << i)
    ]
    assert len(selected_keys) >= 2
    return cosi.combine_keys(selected_keys)


@click.command(name="sign")
@click.option(
    "-o",
    "--outdir",
    type=click.Path(resolve_path=True, file_okay=False, writable=True, path_type=Path),
    default=GENERATED_DEFINITIONS_DIR,
    help="Output directory for generated definitions.",
)
@click.option(
    "-s",
    "--signature",
    help="Signature of the Merkle root.",
)
@click.option("-t", "--test-sign", is_flag=True, help="Sign with test/dev keys.")
@click.option("-v", "--verbose", is_flag=True, help="Display more info.")
def sign_definitions(
    outdir: Path,
    signature: str | None,
    test_sign: bool,
    verbose: bool,
) -> None:
    """Generate signed Ethereum definitions for python-trezor and others.
    If ran without `--publickey` and/or `--signature` it prints the computed Merkle tree root hash.
    If ran with `--publickey` and `--signature` it checks the signed root with generated one and saves the definitions.
    """
    if (
        outdir.is_dir()
        and list(outdir.iterdir())
        and outdir != GENERATED_DEFINITIONS_DIR
        and not click.confirm(
            f"Directory {outdir} is not empty. Contents will be DELETED. Continue?"
        )
    ):
        raise click.Abort()

    assert not outdir.is_file()
    setup_logging(verbose)

    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True)

    # Convert stuff to bytes
    signature_bytes = bytes.fromhex(signature) if signature else None

    # load prepared definitions
    metadata, networks, tokens = load_definitions_data()
    timestamp = metadata["unix_timestamp"]
    loaded_merkle_root = metadata["merkle_root"]

    # serialize definitions
    serializations = serialize_definitions(networks, tokens, timestamp)

    # build Merkle tree
    mt = MerkleTree(serializations.keys())
    root_hash = mt.get_root_hash()
    root_hash_str = root_hash.hex()

    if loaded_merkle_root != root_hash_str:
        raise click.ClickException(
            f"Loaded Merkle tree root hash ({loaded_merkle_root}) does not match computed one ({root_hash_str})."
        )

    print(f"Merkle tree root hash: {root_hash_str}")
    if not test_sign and signature is not None:
        signature_bytes = bytes.fromhex(signature)
        if len(signature_bytes) != 65:
            raise click.ClickException(
                "Provided `--signature` value is not valid. "
                "It should be 65 bytes long."
            )
        publickey_bytes = _combine_public_key(signature_bytes[0])
    elif test_sign:
        # Signing the Merkle tree root hash with test/dev keys
        print("Signing the Merkle tree root hash with test/dev keys...")
        signature_bytes = sign_with_dev_keys(root_hash)
        publickey_bytes = get_dev_public_key()
    else:
        return

    assert signature_bytes is not None

    verify_key = ed25519.VerifyingKey(publickey_bytes)  # type: ignore
    try:
        verify_key.verify(signature_bytes[1:], root_hash)  # type: ignore
    except ed25519.BadSignatureError:  # type: ignore
        raise click.ClickException(
            "Provided `--signature` value is not valid for computed "
            f"Merkle tree root hash ({root_hash_str})."
        )

    # write out the latest signature
    if not test_sign and signature is not None:
        metadata["signature"] = signature_bytes.hex()
    store_definitions_data(metadata, networks, tokens)

    with click.progressbar(serializations.items(), label="Writing definitions") as bar:
        for serialized, item in bar:
            # add proof to serialized definition
            serialized += _make_proof(serialized, mt, signature_bytes)
            if "address" in item:
                item_type = "token"
                gen_func = _generate_token_def
            else:
                item_type = "network"
                gen_func = _generate_network_def

            if len(serialized) > 1024:
                print(f"{item_type} longer than 1024 bytes - {item}")
                continue

            # write definition into directory
            gen_func(item, serialized, outdir)
