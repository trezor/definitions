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

    serialized: NotRequired[bytes]


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

    serialized: NotRequired[bytes]


class DefinitionsFileMetadata(TypedDict):
    datetime: str
    unix_timestamp: int
    commit_hash: str
    merkle_tree_hash: str


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


def get_definitions_merkle_tree_hash(
    networks: list[Network], tokens: list[Token], timestamp: int
) -> str:
    # deepcopying not to add serialized field to the original definitions
    networks, tokens = add_serialized_field_to_definitions(
        deepcopy(networks), deepcopy(tokens), timestamp
    )
    merkle_tree = get_merkle_tree(networks, tokens)
    return merkle_tree.get_root_hash().hex()


def get_merkle_tree(networks: list[Network], tokens: list[Token]) -> MerkleTree:
    return MerkleTree(d["serialized"] for d in networks + tokens)


def add_serialized_field_to_definitions(
    networks: list[Network], tokens: list[Token], timestamp: int
) -> tuple[list[Network], list[Token]]:
    for network in networks:
        ser = _serialize_eth_info(
            EthereumNetworkInfo(
                chain_id=network["chain_id"],
                symbol=network["shortcut"],
                slip44=network["slip44"],
                name=network["name"],
            ),
            EthereumDefinitionType.NETWORK,
            timestamp,
        )
        network["serialized"] = ser
    for token in tokens:
        ser = _serialize_eth_info(
            EthereumTokenInfo(
                address=bytes.fromhex(token["address"][2:]),
                chain_id=token["chain_id"],
                symbol=token["shortcut"],
                decimals=token["decimals"],
                name=token["name"],
            ),
            EthereumDefinitionType.TOKEN,
            timestamp,
        )
        token["serialized"] = ser

    return networks, tokens


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
