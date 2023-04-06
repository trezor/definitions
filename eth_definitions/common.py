from __future__ import annotations

import datetime
import io
import json
import logging
import subprocess
import sys
from collections import OrderedDict
from copy import deepcopy
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, TypedDict

from typing_extensions import NotRequired

import click
from trezorlib import definitions, protobuf
from trezorlib.merkle_tree import MerkleTree
from trezorlib.messages import (
    EthereumDefinitionType,
    EthereumNetworkInfo,
    EthereumTokenInfo,
)

if TYPE_CHECKING:
    from typing import TypeVar

    DEFINITION_TYPE = TypeVar("DEFINITION_TYPE", "Network", "Token")

HERE = Path(__file__).parent
ROOT = HERE.parent

DEFINITIONS_PATH = ROOT / "definitions-latest.json"
GENERATED_DEFINITIONS_DIR = ROOT / "definitions-latest"

CURRENT_TIME = datetime.datetime.now(datetime.timezone.utc)
TIMESTAMP_FORMAT = "%d.%m.%Y %X%z"
CURRENT_UNIX_TIMESTAMP = int(CURRENT_TIME.timestamp())
CURRENT_TIMESTAMP_STR = CURRENT_TIME.strftime(TIMESTAMP_FORMAT)

FORMAT_VERSION_BYTES = b"trzd1"


class ChangeResolutionStrategy(Enum):
    REJECT_ALL_CHANGES = 1
    ACCEPT_ALL_CHANGES = 2
    PROMPT_USER = 3

    @classmethod
    def from_args(
        cls, interactive: bool, force_accept: bool
    ) -> ChangeResolutionStrategy:
        if interactive and force_accept:
            raise ValueError("Cannot be both interactive and force-accept")

        if interactive:
            return cls.PROMPT_USER
        elif force_accept:
            return cls.ACCEPT_ALL_CHANGES
        else:
            return cls.REJECT_ALL_CHANGES


class Network(TypedDict):
    chain: str
    chain_id: int
    is_testnet: bool
    name: str
    shortcut: str  # change later to symbol
    slip44: int

    coingecko_id: NotRequired[str]
    coingecko_rank: NotRequired[bool]
    deleted: NotRequired[bool]


class Token(TypedDict):
    address: str
    chain: str
    chain_id: int
    decimals: int
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: NotRequired[str]
    coingecko_rank: NotRequired[bool]
    deleted: NotRequired[bool]


class DefinitionsFileMetadata(TypedDict):
    datetime: str
    unix_timestamp: int
    merkle_root: str
    signature: NotRequired[str]


class DefinitionsFileFormat(TypedDict):
    networks: list[Network]
    tokens: list[Token]
    metadata: DefinitionsFileMetadata


def setup_logging(verbose: bool):
    log_level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    root.addHandler(handler)


def load_json_file(file: str | Path) -> Any:
    return json.loads(Path(file).read_text(), object_pairs_hook=OrderedDict)


def get_git_commit_hash() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()


def hash_dict_on_keys(d: Network | Token, exclude_keys: Collection[str] = ()) -> bytes:
    """Get the hash of a dict, excluding selected keys."""
    tmp_dict = {k: v for k, v in d.items() if k not in exclude_keys}

    return sha256(json.dumps(tmp_dict, sort_keys=True).encode()).digest()


def get_merkle_root(
    networks: list[Network], tokens: list[Token], timestamp: int
) -> str:
    serializations = serialize_definitions(networks, tokens, timestamp)
    merkle_tree = MerkleTree(serializations.keys())
    return merkle_tree.get_root_hash().hex()


def _serialize_network(network: Network, timestamp: int) -> bytes:
    network_info = EthereumNetworkInfo(
        chain_id=network["chain_id"],
        symbol=network["shortcut"],
        slip44=network["slip44"],
        name=network["name"],
    )
    return _serialize_eth_info(network_info, EthereumDefinitionType.NETWORK, timestamp)


def _serialize_token(token: Token, timestamp: int) -> bytes:
    token_info = EthereumTokenInfo(
        address=bytes.fromhex(token["address"][2:]),
        chain_id=token["chain_id"],
        symbol=token["shortcut"],
        decimals=token["decimals"],
        name=token["name"],
    )
    return _serialize_eth_info(token_info, EthereumDefinitionType.TOKEN, timestamp)


def serialize_definitions(
    networks: list[Network], tokens: list[Token], timestamp: int
) -> dict[bytes, Network | Token]:
    network_bytes = {_serialize_network(n, timestamp): n for n in networks}
    token_bytes = {_serialize_token(t, timestamp): t for t in tokens}
    return {**network_bytes, **token_bytes}


def _serialize_eth_info(
    info: EthereumNetworkInfo | EthereumTokenInfo,
    data_type_num: EthereumDefinitionType,
    timestamp: int,
) -> bytes:
    buf = io.BytesIO()
    protobuf.dump_message(buf, info)
    payload = definitions.DefinitionPayload(
        magic=FORMAT_VERSION_BYTES,
        data_type=data_type_num,
        timestamp=timestamp,
        data=buf.getvalue(),
    )
    return payload.build()


def load_definitions_data(
    path: Path = DEFINITIONS_PATH,
) -> tuple[DefinitionsFileMetadata, list[Network], list[Token]]:
    if not path.is_file():
        raise click.ClickException(
            f'File "{path}" with prepared definitions does not exists.'
        )

    defs_data: DefinitionsFileFormat = load_json_file(path)
    try:
        metadata = defs_data["metadata"]
        networks = defs_data["networks"]
        tokens = defs_data["tokens"]
        return metadata, networks, tokens
    except KeyError:
        raise click.ClickException(
            "File with prepared definitions is not complete. "
            '"metadata", "networks" and "tokens" section may be missing.'
        )


def store_definitions_data(
    metadata: DefinitionsFileMetadata,
    networks: list[Network],
    tokens: list[Token],
    *,
    path: Path = DEFINITIONS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    defs = DefinitionsFileFormat(
        networks=networks,
        tokens=tokens,
        metadata=metadata,
    )

    with open(path, "w") as f:
        json.dump(defs, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")

    logging.info(f"Success - results saved under {path}")


def make_metadata(
    networks: list[Network],
    tokens: list[Token],
    now: datetime.datetime | None = None,
) -> DefinitionsFileMetadata:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = int(now.timestamp())
    time_str = now.isoformat()
    merkle_root = get_merkle_root(networks, tokens, timestamp)
    return DefinitionsFileMetadata(
        datetime=time_str,
        unix_timestamp=timestamp,
        merkle_root=merkle_root,
    )
