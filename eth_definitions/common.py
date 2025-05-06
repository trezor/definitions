from __future__ import annotations

import dataclasses
import datetime
import io
import json
import logging
import subprocess
import sys
from collections import OrderedDict
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection, TypedDict

import click
from trezorlib import definitions, protobuf, tools
from trezorlib.merkle_tree import MerkleTree
from trezorlib.messages import (
    DefinitionType,
    EthereumNetworkInfo,
    EthereumTokenInfo,
)
from typing_extensions import NotRequired


class SolanaTokenInfo(protobuf.MessageType):
    MESSAGE_WIRE_TYPE = None
    FIELDS = {
        1: protobuf.Field("mint", "bytes", repeated=False, required=True),
        2: protobuf.Field("symbol", "string", repeated=False, required=True),
        3: protobuf.Field("name", "string", repeated=False, required=True),
    }

    def __init__(
        self,
        *,
        mint: "bytes",
        symbol: "str",
        name: "str",
    ) -> None:
        self.mint = mint
        self.symbol = symbol
        self.name = name


if TYPE_CHECKING:
    from typing import TypeVar

    DEFINITION_TYPE = TypeVar("DEFINITION_TYPE", "Network", "ERC20Token", "SolanaToken")

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
    coingecko_network_id: NotRequired[str]
    coingecko_rank: NotRequired[bool]
    deleted: NotRequired[bool]


class ERC20Token(TypedDict):
    address: str
    chain: str
    chain_id: int
    decimals: int
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: NotRequired[str]
    coingecko_rank: NotRequired[bool]
    deleted: NotRequired[bool]


class SolanaToken(TypedDict):
    mint: str
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: NotRequired[str]
    coingecko_rank: NotRequired[bool]
    deleted: NotRequired[bool]


class DefinitionsFileMetadata(TypedDict):
    datetime: str
    unix_timestamp: int
    merkle_root: str
    commit_hash: str
    signature: NotRequired[str]


class DefinitionsFileFormat(TypedDict):
    networks: list[Network]
    erc20_tokens: list[ERC20Token]
    solana_tokens: list[SolanaToken]
    metadata: DefinitionsFileMetadata


@dataclasses.dataclass
class DefinitionsData:
    networks: list[Network]
    erc20_tokens: list[ERC20Token]
    solana_tokens: list[SolanaToken]

    @classmethod
    def from_dict(cls, data: DefinitionsFileFormat) -> "DefinitionsData":
        return cls(
            networks=data["networks"],
            erc20_tokens=data["erc20_tokens"],
            solana_tokens=data["solana_tokens"],
        )

    def to_dict(self, metadata: DefinitionsFileMetadata) -> DefinitionsFileFormat:
        return {
            "networks": self.networks,
            "erc20_tokens": self.erc20_tokens,
            "solana_tokens": self.solana_tokens,
            "metadata": metadata,
        }


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


def hash_dict_on_keys(
    d: Network | ERC20Token | SolanaToken, exclude_keys: Collection[str] = ()
) -> bytes:
    """Get the hash of a dict, excluding selected keys."""
    tmp_dict = {k: v for k, v in d.items() if k not in exclude_keys}
    return sha256(json.dumps(tmp_dict, sort_keys=True).encode()).digest()


def get_merkle_root(definitions_data: DefinitionsData, timestamp: int) -> str:
    serializations = serialize_definitions(definitions_data, timestamp)
    merkle_tree = MerkleTree(serializations.keys())
    return merkle_tree.get_root_hash().hex()


def _serialize_eth_network(network: Network, timestamp: int) -> bytes:
    network_info = EthereumNetworkInfo(
        chain_id=network["chain_id"],
        symbol=network["shortcut"],
        slip44=network["slip44"],
        name=network["name"],
    )
    return _encode_payload(network_info, DefinitionType.ETHEREUM_NETWORK, timestamp)


def _serialize_eth_token(token: ERC20Token, timestamp: int) -> bytes:
    token_info = EthereumTokenInfo(
        address=bytes.fromhex(token["address"][2:]),
        chain_id=token["chain_id"],
        symbol=token["shortcut"],
        decimals=token["decimals"],
        name=token["name"],
    )
    return _encode_payload(token_info, DefinitionType.ETHEREUM_TOKEN, timestamp)


def _serialize_solana_token(token: SolanaToken, timestamp: int) -> bytes:
    try:
        token_info = SolanaTokenInfo(
            mint=tools.b58decode(token["mint"]),
            symbol=token["shortcut"],
            name=token["name"],
        )
    except Exception as e:
        print(f"Error serializing solana token: {e}")
        print(token)
        raise e

    return _encode_payload(token_info, DefinitionType.SOLANA_TOKEN, timestamp)


def serialize_definitions(
    definitions_data: DefinitionsData, timestamp: int
) -> dict[bytes, Network | ERC20Token | SolanaToken]:
    network_bytes = {
        _serialize_eth_network(n, timestamp): n for n in definitions_data.networks
    }
    erc20_token_bytes = {
        _serialize_eth_token(t, timestamp): t for t in definitions_data.erc20_tokens
    }
    solana_token_bytes = {
        _serialize_solana_token(t, timestamp): t for t in definitions_data.solana_tokens
    }
    return {**network_bytes, **erc20_token_bytes, **solana_token_bytes}


def _encode_payload(
    info: EthereumNetworkInfo | EthereumTokenInfo | SolanaTokenInfo,
    data_type_num: DefinitionType,
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
) -> tuple[DefinitionsFileMetadata, DefinitionsData]:
    if not path.is_file():
        raise click.ClickException(
            f'File "{path}" with prepared definitions does not exists.'
        )

    defs_data: DefinitionsFileFormat = load_json_file(path)
    try:
        metadata = defs_data["metadata"]
        definitions_data = DefinitionsData.from_dict(defs_data)
        return metadata, definitions_data
    except KeyError:
        raise click.ClickException(
            "File with prepared definitions is not complete. "
            '"metadata", "networks", "erc20_tokens" and "solana_tokens" sections may be missing.'
        )


def store_definitions_data(
    metadata: DefinitionsFileMetadata,
    definitions_data: DefinitionsData,
    *,
    path: Path = DEFINITIONS_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    defs = definitions_data.to_dict(metadata)

    with open(path, "w") as f:
        json.dump(defs, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")

    logging.info(f"Success - results saved under {path}")


def make_metadata(
    definitions_data: DefinitionsData, now: datetime.datetime | None = None
) -> DefinitionsFileMetadata:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = int(now.timestamp())
    time_str = now.isoformat()
    merkle_root = get_merkle_root(definitions_data, timestamp)
    return DefinitionsFileMetadata(
        datetime=time_str,
        unix_timestamp=timestamp,
        merkle_root=merkle_root,
        commit_hash=get_git_commit_hash(),
    )
