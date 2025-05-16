#!/usr/bin/env python3
from __future__ import annotations

import io
import logging
import shutil
import typing as t
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click
from cryptography.exceptions import InvalidSignature
from trezorlib.merkle_tree import MerkleTree

from .common import (
    GENERATED_DEFINITIONS_DIR,
    Network,
    ERC20Token,
    SolanaToken,
    DefinitionsData,
    load_definitions_data,
    serialize_definitions,
    setup_logging,
)
from . import crypto

LOG = logging.getLogger(__name__)


# ====== definitions tools ======


@dataclass(frozen=True)
class OutputPath:
    path: tuple[str, ...]
    exists_ok: bool = False

    @classmethod
    def from_item(cls, item: Network | ERC20Token | SolanaToken) -> t.Iterator[t.Self]:
        if "address" in item:
            address = item["address"][2:].lower()
            yield cls(
                ("eth", "chain-id", str(item["chain_id"]), f"token-{address}.dat")
            )
        elif "mint" in item:
            mint = item["mint"]
            yield cls(("solana", "token", f"{mint}.dat"))
        else:
            yield cls(("eth", "chain-id", str(item["chain_id"]), "network.dat"))
            yield cls(
                ("eth", "slip44", str(item["slip44"]), "network.dat"), exists_ok=True
            )


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


def serialize_with_progress(
    definitions_data: DefinitionsData, timestamp: int
) -> dict[bytes, Network | ERC20Token | SolanaToken]:
    with click.progressbar(
        length=len(definitions_data.networks)
        + len(definitions_data.erc20_tokens)
        + len(definitions_data.solana_tokens),
        label="Serializing definitions",
    ) as bar:
        return serialize_definitions(definitions_data, timestamp, progress=bar.update)


def _archive_dir(
    t: tarfile.TarFile,
    src_dir: Path,
    dest_prefix: str,
    progress: t.Callable[[int], None],
) -> None:
    for item in src_dir.glob("**/*"):
        progress(1)
        if not item.is_file():
            continue
        relpath = item.relative_to(src_dir)
        t.add(item, arcname=f"{dest_prefix}{relpath}")


def create_deploy_tar(src_dir: Path, out_file: Path) -> None:
    total_items = sum(1 for _ in src_dir.glob("**/*"))
    eth_items = sum(1 for _ in (src_dir / "eth").glob("**/*"))

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. archive contents of src_dir into future definitions/definitions.tar.xz
        defs_tar = Path(tmpdir) / "definitions.tar.xz"
        with (
            click.progressbar(
                length=total_items, label="definitions/definitions.tar.xz"
            ) as bar,
            tarfile.open(defs_tar, "w:xz") as f,
        ):
            _archive_dir(f, src_dir, "", bar.update)

        # 2. archive contents of src_dir/eth into future eth-definitions/definitions.tar.xz
        eth_defs_tar = Path(tmpdir) / "eth-definitions.tar.xz"
        with (
            click.progressbar(
                length=eth_items, label="eth-definitions/definitions.tar.xz"
            ) as bar,
            tarfile.open(eth_defs_tar, "w:xz") as f,
        ):
            _archive_dir(f, src_dir / "eth", "", bar.update)

        with (
            click.progressbar(
                length=total_items + eth_items + 2, label="deploy.tar.xz"
            ) as bar,
            tarfile.open(out_file, "w:xz") as f,
        ):
            # 3. write contents of src_dir into out_file:/definitions
            _archive_dir(f, src_dir, "definitions/", bar.update)

            # 4. write contents of src_dir/eth into out_file:/eth-definitions
            _archive_dir(f, src_dir / "eth", "eth-definitions/", bar.update)

            # 5. write definitions.tar.xz into out_file:/definitions/definitions.tar.xz
            f.add(defs_tar, arcname="definitions/definitions.tar.xz")
            bar.update(1)

            # 6. write eth-definitions.tar.xz into out_file:/eth-definitions/definitions.tar.xz
            f.add(eth_defs_tar, arcname="eth-definitions/definitions.tar.xz")
            bar.update(1)


@click.command(name="generate")
@click.option(
    "-o",
    "--outdir",
    type=click.Path(resolve_path=True, file_okay=False, writable=True, path_type=Path),
    default=GENERATED_DEFINITIONS_DIR,
    help="Output directory for generated definitions.",
)
@click.option("-d", "--dev-sign", is_flag=True, help="Sign with dev keys.")
@click.option("-v", "--verbose", is_flag=True, help="Display more info.")
def generate_definitions(
    outdir: Path,
    dev_sign: bool,
    verbose: bool,
) -> None:
    """Generate binary token definitions for python-trezor and others.

    If ran without `--dev-sign` it will use the signature from metadata if available.
    If ran with `--dev-sign` it will sign with development keys.
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

    # load prepared definitions
    metadata, definitions_data = load_definitions_data()
    timestamp = metadata["unix_timestamp"]
    loaded_merkle_root = metadata["merkle_root"]

    # serialize definitions
    serializations = serialize_with_progress(definitions_data, timestamp)

    # build Merkle tree
    mt = MerkleTree(serializations.keys())
    root_hash = mt.get_root_hash()
    root_hash_str = root_hash.hex()

    if loaded_merkle_root != root_hash_str:
        raise click.ClickException(
            f"Loaded Merkle tree root hash ({loaded_merkle_root}) does not match computed one ({root_hash_str})."
        )

    print(f"Merkle tree root hash: {root_hash_str}")

    if dev_sign:
        # Signing the Merkle tree root hash with dev keys
        print("Signing the Merkle tree root hash with dev keys...")
        signature_bytes = crypto.sign_with_dev_keys(root_hash)
    elif "signature" in metadata:
        # Use the signature from the loaded definitions
        print("Using signature stored in metadata...")
        signature_bytes = bytes.fromhex(metadata["signature"])
    else:
        raise click.ClickException(
            "No signature available. Either use --dev-sign or ensure metadata contains a signature."
        )

    try:
        crypto.verify_signature(signature_bytes, root_hash, dev=dev_sign)
    except InvalidSignature:
        raise click.ClickException(
            "Signature is not valid for computed "
            f"Merkle tree root hash ({root_hash_str})."
        )

    with click.progressbar(serializations.items(), label="Writing definitions") as bar:
        for serialized, item in bar:
            # add proof to serialized definition
            serialized += crypto.make_proof(serialized, mt, signature_bytes)
            for out in OutputPath.from_item(item):
                dest = outdir.joinpath(*out.path)
                _write_path(dest, serialized, out.exists_ok)

            if len(serialized) > 1024:
                print(f"serialization longer than 1024 bytes - {item}")
                continue

    create_deploy_tar(outdir, outdir / "deploy.tar.xz")
