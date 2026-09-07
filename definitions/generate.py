#!/usr/bin/env python3
from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
import typing as t
from dataclasses import dataclass
from pathlib import Path

import click
from cryptography.exceptions import InvalidSignature
from trezorlib import definitions as trezorlib_definitions
from trezorlib import merkle_tree
from trezorlib.merkle_tree import MerkleTree

from . import crypto
from .common import (
    DefinitionsData,
    generated_definitions_dir,
    load_definitions_data,
    resolve_default_version,
    setup_logging,
    validate_version,
)
from .ethereum.types import ERC20DisplayFormat, ERC20Token, Network
from .serialize import serialize_definitions
from .solana.types import SolanaToken

LOG = logging.getLogger(__name__)


# ====== definitions tools ======


@dataclass(frozen=True)
class OutputPath:
    path: tuple[str, ...]
    exists_ok: bool = False

    @classmethod
    def from_item(
        cls, item: Network | ERC20Token | SolanaToken | ERC20DisplayFormat
    ) -> t.Iterator[t.Self]:
        # Display formats must be checked before tokens — both carry "address",
        # but display formats also carry "func_sig".
        if "func_sig" in item:
            address = item["address"][2:].lower()
            func_sig = item["func_sig"][2:].lower()
            yield cls(
                (
                    "eth",
                    "chain-id",
                    str(item["chain_id"]),
                    "display-format",
                    f"{address}-{func_sig}.dat",
                )
            )
        elif "address" in item:
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
    definitions_data: DefinitionsData, timestamp: int, version: int
) -> dict[bytes, Network | ERC20Token | SolanaToken | ERC20DisplayFormat]:
    with click.progressbar(
        length=len(definitions_data.networks)
        + len(definitions_data.erc20_tokens)
        + len(definitions_data.solana_tokens)
        + len(definitions_data.erc20_display_formats),
        label="Serializing definitions",
    ) as bar:
        return serialize_definitions(
            definitions_data, timestamp, version, progress=bar.update
        )


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
                length=total_items + eth_items + 2, label=out_file.name
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
    default=None,
    help="Output directory for generated definitions. "
    "Defaults to definitions-latest-v<version>.",
)
@click.option("-d", "--dev-sign", is_flag=True, help="Sign with dev keys.")
@click.option(
    "--version",
    type=int,
    default=None,
    help="Version of the definitions blob encoded behind magic. "
    "Defaults to the sole active version.",
)
@click.option("-v", "--verbose", is_flag=True, help="Display more info.")
def generate_definitions(
    outdir: Path | None,
    dev_sign: bool,
    version: int | None,
    verbose: bool,
) -> None:
    """Generate binary token definitions for python-trezor and others.

    If ran without `--dev-sign` it will use the signature from metadata if available.
    If ran with `--dev-sign` it will sign with development keys.
    """
    if version is None:
        version = resolve_default_version()
    validate_version(version)
    if outdir is None:
        outdir = generated_definitions_dir(version)
    if (
        outdir.is_dir()
        and list(outdir.iterdir())
        and outdir != generated_definitions_dir(version)
        and not click.confirm(
            f"Directory {outdir} is not empty. Contents will be DELETED. Continue?"
        )
    ):
        raise click.Abort()

    assert not outdir.is_file()
    setup_logging(verbose)

    shutil.rmtree(outdir, ignore_errors=True)
    outdir.mkdir(parents=True)

    # load prepared definitions (metadata for the requested version)
    metadata, definitions_data = load_definitions_data(version)
    timestamp = metadata["unix_timestamp"]
    loaded_merkle_root = metadata["merkle_root"]

    # serialize definitions
    serializations = serialize_with_progress(definitions_data, timestamp, version)

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
        tar_filename = f"deploy_dev_v{version}_{timestamp}.tar.xz"
    elif "signature" in metadata:
        # Use the signature from the loaded definitions
        print("Using signature stored in metadata...")
        signature_bytes = bytes.fromhex(metadata["signature"])
        tar_filename = f"deploy_prod_v{version}_{timestamp}.tar.xz"
    else:
        raise click.ClickException(
            "No signature available. Either use --dev-sign or ensure metadata contains a signature."
        )

    try:
        crypto.verify_signature(signature_bytes, root_hash, version, dev=dev_sign)
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

    create_deploy_tar(outdir, outdir / tar_filename)


def validate_generated_dir(outdir: Path, version: int, dev: bool = False) -> int:
    """Parse and verify every generated definition; return their count.

    Uses trezorlib's own `Definition` parser and verifier, so format drift
    between this repo's encoders and the trezorlib decoders fails loudly here
    instead of on client devices.

    Every definition is parsed and its Merkle proof evaluated against the
    single shared root of the whole directory. Signatures are verified once
    per unique value — pure-python ed25519 verification is too slow to repeat
    per definition.
    """
    paths = sorted(outdir.rglob("*.dat"))
    if not paths:
        raise click.ClickException(
            f"No definitions found in {outdir}. Run `generate --version {version}` first."
        )
    expected_version = str(version).encode("ascii")
    # all definitions of one generate run share a single Merkle root
    expected_root: bytes | None = None
    expected_root_path: Path | None = None
    # signature -> (parsed definition, first path seen with it); signatures
    # are verified once per unique value after the loop (pure-python ed25519
    # verification is too slow to repeat per definition)
    unique_sigs: dict[bytes, tuple[t.Any, Path]] = {}
    with click.progressbar(paths, label="Validating definitions") as bar:
        for path in bar:
            try:
                definition = trezorlib_definitions.Definition.parse(path.read_bytes())
            except Exception as e:
                raise click.ClickException(f"Failed to parse {path}: {e}")
            if definition.payload.version != expected_version:
                raise click.ClickException(
                    f"{path}: payload version {definition.payload.version!r}, "
                    f"expected {expected_version!r}."
                )
            root = merkle_tree.evaluate_proof(
                definition.payload.build(), definition.proof
            )
            if expected_root is None:
                expected_root, expected_root_path = root, path
            elif root != expected_root:
                raise click.ClickException(
                    f"{path} evaluates to a different Merkle root than "
                    f"{expected_root_path}. The directory mixes definitions "
                    "from different generate runs."
                )
            signature = bytes([definition.sigmask]) + definition.signature
            unique_sigs.setdefault(signature, (definition, path))

    assert expected_root is not None  # paths is non-empty
    for signature, (definition, path) in unique_sigs.items():
        try:
            if dev:
                # trezorlib's dev verification expects a single dev key
                # (sigmask 0b001) while this repo dev-signs with all three
                # dev keys (sigmask 0b111), so verify dev signatures with
                # the repo's own crypto instead.
                crypto.verify_signature(signature, expected_root, version, dev=True)
            else:
                definition.verify()
        except Exception as e:
            raise click.ClickException(f"Invalid signature in {path}: {e}")
    return len(paths)


@click.command(name="validate")
@click.option(
    "-d",
    "--dev",
    is_flag=True,
    help="Verify against dev keys instead of production keys.",
)
@click.option(
    "--version",
    type=int,
    default=None,
    help="Definitions format version. Defaults to the sole active version.",
)
@click.option("-v", "--verbose", is_flag=True, help="Display more info.")
def validate_generated_definitions(
    dev: bool,
    version: int | None,
    verbose: bool,
) -> None:
    """Verify generated binary definitions against trezorlib's parser and keys.

    Parses every definition under definitions-latest-v<version>/ and verifies
    its payload version and CoSi signature (production keys, or dev keys with
    --dev).
    """
    setup_logging(verbose)
    if version is None:
        version = resolve_default_version()
    validate_version(version)
    outdir = generated_definitions_dir(version)
    if not outdir.is_dir():
        raise click.ClickException(
            f"Directory {outdir} does not exist. Run `generate --version {version}` first."
        )
    count = validate_generated_dir(outdir, version, dev=dev)
    keys = "dev" if dev else "production"
    click.echo(f"Validated {count} definitions in {outdir} against {keys} keys.")
