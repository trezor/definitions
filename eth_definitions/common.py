from __future__ import annotations

import dataclasses
import datetime
import io
import json
import logging
import subprocess
import sys
import typing as t
from collections import OrderedDict
from enum import Enum
from hashlib import sha256
from pathlib import Path

import click
from trezorlib import definitions, protobuf, tools
from trezorlib.merkle_tree import MerkleTree
from trezorlib.messages import (
    DefinitionType,
    EthereumABITupleInfo,
    EthereumABIType,
    EthereumABIValueInfo,
    EthereumDisplayFormatInfo,
    EthereumERC7730ContainerPath,
    EthereumERC7730FieldFormatterType,
    EthereumERC7730FieldInfo,
    EthereumERC7730Path,
    EthereumNetworkInfo,
    EthereumTokenInfo,
)


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


if t.TYPE_CHECKING:
    from typing import TypeVar

    DEFINITION_TYPE = TypeVar(
        "DEFINITION_TYPE", "Network", "ERC20Token", "SolanaToken", "ERC20DisplayFormat"
    )

HERE = Path(__file__).parent
ROOT = HERE.parent

DEFINITIONS_PATH = ROOT / "definitions-latest.json"
GENERATED_DEFINITIONS_DIR = ROOT / "definitions-latest"
DEPLOY_DEFINITIONS_TAR = ROOT / "definitions-deploy.tar.xz"

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


class Network(t.TypedDict):
    chain: str
    chain_id: int
    is_testnet: bool
    name: str
    shortcut: str  # change later to symbol
    slip44: int

    coingecko_id: t.NotRequired[str]
    coingecko_network_id: t.NotRequired[str]
    coingecko_rank: t.NotRequired[bool]
    deleted: t.NotRequired[bool]


class ERC20Token(t.TypedDict):
    address: str
    chain: str
    chain_id: int
    decimals: int
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: t.NotRequired[str]
    coingecko_rank: t.NotRequired[bool]
    deleted: t.NotRequired[bool]


class SolanaToken(t.TypedDict):
    mint: str
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: t.NotRequired[str]
    coingecko_rank: t.NotRequired[bool]
    deleted: t.NotRequired[bool]


class ABITuple(t.TypedDict):
    fields: list["ABIValue"]
    is_dynamic: bool


class _AtomicABI(t.TypedDict):
    atomic: str


class _DynamicABI(t.TypedDict):
    dynamic: str


class _TupleABI(t.TypedDict):
    tuple: ABITuple


class _ArrayABI(t.TypedDict):
    array: "ABIValue"


ABIValue = _AtomicABI | _DynamicABI | _TupleABI | _ArrayABI


class _ContainerPath(t.TypedDict):
    container_path: str  # "FROM" | "VALUE" | "TO"


class _DataPath(t.TypedDict):
    path: list[int]


ERC7730Path = _ContainerPath | _DataPath


class ERC7730Field(t.TypedDict):
    path: ERC7730Path
    label: str
    formatter: str  # e.g. "FORMATTER_ADDRESS_NAME"

    # TokenAmountFormatter params
    token_path: t.NotRequired[ERC7730Path]
    threshold: t.NotRequired[str]  # hex (no 0x prefix)

    # UnitFormatter params
    decimals: t.NotRequired[int]
    base: t.NotRequired[str]
    prefix: t.NotRequired[bool]


class ERC20DisplayFormat(t.TypedDict):
    chain_id: int
    address: str  # 0x-prefixed lowercase hex (20 bytes)
    func_sig: str  # 0x-prefixed lowercase hex (4 bytes)
    intent: str
    parameter_definitions: list[ABIValue]
    field_definitions: list[ERC7730Field]

    deleted: t.NotRequired[bool]


class DefinitionsFileMetadata(t.TypedDict):
    datetime: str
    unix_timestamp: int
    merkle_root: str
    commit_hash: str
    signature: t.NotRequired[str]


class DefinitionsFileFormat(t.TypedDict):
    networks: list[Network]
    erc20_tokens: list[ERC20Token]
    solana_tokens: list[SolanaToken]
    erc20_display_formats: list[ERC20DisplayFormat]
    metadata: DefinitionsFileMetadata


@dataclasses.dataclass
class DefinitionsData:
    networks: list[Network]
    erc20_tokens: list[ERC20Token]
    solana_tokens: list[SolanaToken]
    erc20_display_formats: list[ERC20DisplayFormat]

    @classmethod
    def from_dict(cls, data: DefinitionsFileFormat) -> "DefinitionsData":
        return cls(
            networks=data["networks"],
            erc20_tokens=data["erc20_tokens"],
            solana_tokens=data["solana_tokens"],
            erc20_display_formats=data["erc20_display_formats"],
        )

    def to_dict(self, metadata: DefinitionsFileMetadata) -> DefinitionsFileFormat:
        return {
            "networks": self.networks,
            "erc20_tokens": self.erc20_tokens,
            "solana_tokens": self.solana_tokens,
            "erc20_display_formats": self.erc20_display_formats,
            "metadata": metadata,
        }


def setup_logging(verbose: bool):
    log_level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    root.addHandler(handler)


def load_json_file(file: str | Path) -> t.Any:
    return json.loads(Path(file).read_text(), object_pairs_hook=OrderedDict)


def get_git_commit_hash() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()


def hash_dict_on_keys(
    d: Network | ERC20Token | SolanaToken | ERC20DisplayFormat,
    exclude_keys: t.Collection[str] = (),
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


_ABI_VARIANT_KEYS = frozenset({"atomic", "dynamic", "tuple", "array"})
_PATH_VARIANT_KEYS = frozenset({"container_path", "path"})


def _build_abi_value_info(d: ABIValue) -> EthereumABIValueInfo:
    variants = _ABI_VARIANT_KEYS & d.keys()
    if len(variants) != 1:
        raise ValueError(
            f"ABIValue must have exactly one variant key, got {sorted(variants)}: {d}"
        )
    if "atomic" in d:
        return EthereumABIValueInfo(atomic=EthereumABIType[d["atomic"]])
    if "dynamic" in d:
        return EthereumABIValueInfo(dynamic=EthereumABIType[d["dynamic"]])
    if "tuple" in d:
        tup = d["tuple"]
        return EthereumABIValueInfo(
            tuple=EthereumABITupleInfo(
                fields=[_build_abi_value_info(f) for f in tup["fields"]],
                is_dynamic=tup["is_dynamic"],
            )
        )
    if "array" in d:
        return EthereumABIValueInfo(array=_build_abi_value_info(d["array"]))
    raise AssertionError("unreachable")


def _build_erc7730_path(d: ERC7730Path) -> EthereumERC7730Path:
    variants = _PATH_VARIANT_KEYS & d.keys()
    if len(variants) != 1:
        raise ValueError(
            f"ERC7730Path must have exactly one variant key, got {sorted(variants)}: {d}"
        )
    if "container_path" in d:
        return EthereumERC7730Path(
            container_path=EthereumERC7730ContainerPath[d["container_path"]]
        )
    if "path" in d:
        return EthereumERC7730Path(path=list(d["path"]))
    raise AssertionError("unreachable")


def _build_erc7730_field_info(d: ERC7730Field) -> EthereumERC7730FieldInfo:
    return EthereumERC7730FieldInfo(
        path=_build_erc7730_path(d["path"]),
        label=d["label"],
        formatter=EthereumERC7730FieldFormatterType[d["formatter"]],
        token_path=(
            _build_erc7730_path(d["token_path"]) if "token_path" in d else None
        ),
        threshold=bytes.fromhex(d["threshold"]) if "threshold" in d else None,
        decimals=d.get("decimals"),
        base=d.get("base"),
        prefix=d.get("prefix"),
    )


def _strip_0x(label: str, value: str) -> str:
    if not value.startswith("0x"):
        raise ValueError(f"{label} must start with '0x', got {value!r}")
    return value[2:]


def _serialize_eth_display_format(
    display_format: ERC20DisplayFormat, timestamp: int
) -> bytes:
    info = EthereumDisplayFormatInfo(
        chain_id=display_format["chain_id"],
        address=bytes.fromhex(_strip_0x("address", display_format["address"])),
        func_sig=bytes.fromhex(_strip_0x("func_sig", display_format["func_sig"])),
        intent=display_format["intent"],
        parameter_definitions=[
            _build_abi_value_info(p) for p in display_format["parameter_definitions"]
        ],
        field_definitions=[
            _build_erc7730_field_info(f) for f in display_format["field_definitions"]
        ],
    )
    return _encode_payload(info, DefinitionType.ETHEREUM_DISPLAY_FORMAT, timestamp)


def serialize_definitions(
    definitions_data: DefinitionsData,
    timestamp: int,
    progress: t.Callable[[int], None] = lambda _: None,
) -> dict[bytes, Network | ERC20Token | SolanaToken | ERC20DisplayFormat]:
    T = t.TypeVar("T")

    def wrap(i: t.Iterable[T]) -> t.Iterator[T]:
        for item in i:
            yield item
            progress(1)

    network_bytes = {
        _serialize_eth_network(n, timestamp): n for n in wrap(definitions_data.networks)
    }
    erc20_token_bytes = {
        _serialize_eth_token(t, timestamp): t
        for t in wrap(definitions_data.erc20_tokens)
    }
    solana_token_bytes = {
        _serialize_solana_token(t, timestamp): t
        for t in wrap(definitions_data.solana_tokens)
    }
    display_format_bytes = {
        _serialize_eth_display_format(df, timestamp): df
        for df in wrap(definitions_data.erc20_display_formats)
    }
    return {
        **network_bytes,
        **erc20_token_bytes,
        **solana_token_bytes,
        **display_format_bytes,
    }


def _encode_payload(
    info: EthereumNetworkInfo
    | EthereumTokenInfo
    | SolanaTokenInfo
    | EthereumDisplayFormatInfo,
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
