import click
import pytest
from trezorlib.merkle_tree import MerkleTree

from . import crypto
from .common import DefinitionsData
from .generate import OutputPath, validate_generated_dir
from .serialize import serialize_definitions


@pytest.fixture
def definitions_data() -> DefinitionsData:
    return DefinitionsData(
        networks=[
            {
                "chain": "eth",
                "chain_id": 1,
                "is_testnet": False,
                "name": "Ethereum",
                "shortcut": "ETH",
                "slip44": 60,
            }
        ],
        erc20_tokens=[],
        solana_tokens=[],
        erc20_display_formats=[],
    )


def _write_dev_signed_dir(outdir, definitions_data, version=1, timestamp=1234567890):
    """Replicate what `generate --dev-sign` produces, into `outdir`."""
    serializations = serialize_definitions(definitions_data, timestamp, version)
    mt = MerkleTree(serializations.keys())
    signature = crypto.sign_with_dev_keys(mt.get_root_hash())
    for serialized, item in serializations.items():
        blob = serialized + crypto.make_proof(serialized, mt, signature)
        for out in OutputPath.from_item(item):
            dest = outdir.joinpath(*out.path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                continue
            dest.write_bytes(blob)
    return len(serializations)


def test_validate_dev_signed(tmp_path, definitions_data):
    count = _write_dev_signed_dir(tmp_path, definitions_data)
    assert validate_generated_dir(tmp_path, 1, dev=True) > 0
    assert count > 0


def test_validate_dev_signed_fails_against_production_keys(
    tmp_path, definitions_data
):
    _write_dev_signed_dir(tmp_path, definitions_data)
    with pytest.raises(click.ClickException, match="Invalid signature"):
        validate_generated_dir(tmp_path, 1, dev=False)


def test_validate_rejects_version_mismatch(tmp_path, definitions_data):
    # directory containing v2 payloads validated as v1
    _write_dev_signed_dir(tmp_path, definitions_data, version=2)
    with pytest.raises(click.ClickException, match="payload version"):
        validate_generated_dir(tmp_path, 1, dev=True)


def test_validate_empty_dir_fails(tmp_path):
    with pytest.raises(click.ClickException, match="No definitions found"):
        validate_generated_dir(tmp_path, 1, dev=True)


def test_validate_rejects_garbage(tmp_path, definitions_data):
    _write_dev_signed_dir(tmp_path, definitions_data)
    bad = tmp_path / "eth" / "chain-id" / "1" / "token-deadbeef.dat"
    bad.write_bytes(b"garbage")
    with pytest.raises(click.ClickException, match="Failed to parse"):
        validate_generated_dir(tmp_path, 1, dev=True)


def test_validate_rejects_mixed_roots(tmp_path, definitions_data):
    _write_dev_signed_dir(tmp_path, definitions_data, timestamp=1111111111)
    other = tmp_path / "other"
    _write_dev_signed_dir(other, definitions_data, timestamp=2222222222)

    # sneak in a definition signed over a different Merkle root
    stray = other / "eth" / "chain-id" / "1" / "network.dat"
    dest = tmp_path / "eth" / "chain-id" / "2"
    dest.mkdir(parents=True)
    (dest / "network.dat").write_bytes(stray.read_bytes())

    with pytest.raises(click.ClickException, match="different Merkle root"):
        validate_generated_dir(tmp_path, 1, dev=True)
