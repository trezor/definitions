import json

import click
import pytest

from . import common
from .common import (
    DefinitionsData,
    DefinitionsFileMetadata,
    load_definitions_data,
    metadata_path,
    resolve_default_version,
    store_definitions_data,
    store_metadata,
    validate_version,
)


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    monkeypatch.setattr(
        common, "DEFINITIONS_PATH", tmp_path / "definitions-latest.json"
    )
    return tmp_path


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


def _metadata(version: int = 1) -> DefinitionsFileMetadata:
    return DefinitionsFileMetadata(
        datetime="2026-08-27T00:00:00+00:00",
        unix_timestamp=1780000000,
        merkle_root="00" * 32,
        commit_hash="abcd",
        version=version,
    )


# ====== version helpers ======


@pytest.mark.parametrize("version", common.ACTIVE_VERSIONS)
def test_validate_version_accepts_active(version):
    assert validate_version(version) == version


def test_validate_version_rejects_inactive():
    inactive_version = max(common.ACTIVE_VERSIONS) + 1
    with pytest.raises(click.ClickException, match="Unsupported definitions version"):
        validate_version(inactive_version)


def test_resolve_default_version():
    if len(common.ACTIVE_VERSIONS) == 1:
        assert resolve_default_version() == common.ACTIVE_VERSIONS[0]
    else:
        with pytest.raises(click.ClickException, match="--version"):
            resolve_default_version()


# ====== store/load roundtrip ======


def test_store_and_load_roundtrip(tmp_root, definitions_data):
    store_definitions_data(definitions_data)
    store_metadata(_metadata(version=1))

    assert (tmp_root / "definitions-latest.json").is_file()
    v1_path = tmp_root / "definitions-latest-metadata-v1.json"
    assert v1_path.is_file()

    # coin data contains only the four sections, no embedded metadata
    stored = json.loads((tmp_root / "definitions-latest.json").read_text())
    assert "metadata" not in stored
    assert stored.keys() == {
        "networks",
        "erc20_tokens",
        "solana_tokens",
        "erc20_display_formats",
    }

    metadata, loaded = load_definitions_data(1)
    assert metadata["version"] == 1
    assert metadata["merkle_root"] == "00" * 32
    assert loaded.networks == definitions_data.networks


def test_load_definitions_data_default_resolves_sole_active(tmp_root, definitions_data):
    store_definitions_data(definitions_data)
    store_metadata(_metadata(version=common.ACTIVE_VERSIONS[0]))

    if len(common.ACTIVE_VERSIONS) == 1:
        metadata, loaded = load_definitions_data()
        assert metadata["version"] == common.ACTIVE_VERSIONS[0]
        assert loaded.erc20_display_formats == []
    else:
        with pytest.raises(click.ClickException, match="--version"):
            load_definitions_data()


def test_load_definitions_data_rejects_version_mismatch(
    tmp_root, definitions_data, monkeypatch
):
    monkeypatch.setattr(common, "ACTIVE_VERSIONS", (1, 2))
    store_definitions_data(definitions_data)
    store_metadata(_metadata(version=1))

    # simulate a misnamed metadata file: v2 file claiming version 1
    v2 = tmp_root / "definitions-latest-metadata-v2.json"
    v2.write_text(json.dumps(_metadata(version=1)))

    with pytest.raises(click.ClickException, match="expected 2"):
        load_definitions_data(2)


def test_load_definitions_data_missing_metadata_file(tmp_root, definitions_data):
    store_definitions_data(definitions_data)

    with pytest.raises(click.ClickException, match="does not exist"):
        load_definitions_data(1)


def test_store_metadata_writes_per_version_files(tmp_root):
    for version in common.ACTIVE_VERSIONS:
        store_metadata(_metadata(version=version))
        assert metadata_path(version).is_file()
        stored = json.loads(metadata_path(version).read_text())
        assert stored["version"] == version


# ====== payload header (magic + version) ======


@pytest.mark.parametrize("version", common.ACTIVE_VERSIONS)
def test_payload_header_contains_magic_and_version(version):
    from .ethereum.serialize import serialize_token

    token = {
        "address": "0x" + "ab" * 20,
        "chain_id": 1,
        "shortcut": "ABC",
        "decimals": 18,
        "name": "Test Token",
    }
    serialized = serialize_token(token, 1234567890, version)
    assert serialized[:4] == b"trzd"
    assert serialized[4:5] == str(version).encode("ascii")
