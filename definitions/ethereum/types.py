"""Ethereum-specific definition types (networks, ERC-20 tokens, display formats)."""

from __future__ import annotations

import typing as t


class Network(t.TypedDict):
    chain: str
    chain_id: int
    is_testnet: bool
    name: str
    shortcut: str  # change later to symbol
    slip44: int

    coingecko_id: t.NotRequired[str]
    coingecko_network_id: t.NotRequired[str]
    coingecko_rank: t.NotRequired[int]
    deleted: t.NotRequired[bool]


class ERC20Token(t.TypedDict):
    address: str
    chain: str
    chain_id: int
    decimals: int
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: t.NotRequired[str]
    coingecko_rank: t.NotRequired[int]
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
    # trailing byte slice of the walked value (e.g. `token.[-20:]`)
    slice_start: t.NotRequired[int]
    slice_end: t.NotRequired[int]


class _ConstValuePath(t.TypedDict):
    const_value: str  # a literal constant value, not walked from calldata


ERC7730Path = _ContainerPath | _DataPath | _ConstValuePath


class ERC7730EnumValue(t.TypedDict):
    key: int  # the decoded calldata value (uint32)
    value: str  # what the user sees for it


class ERC7730Field(t.TypedDict):
    path: ERC7730Path
    label: str
    formatter: str  # e.g. "FORMATTER_ADDRESS_NAME"

    # TokenAmountFormatter params
    token_path: t.NotRequired[ERC7730Path]
    threshold: t.NotRequired[str]  # hex (no 0x prefix)
    const_token_address: t.NotRequired[str]  # hex (no 0x prefix), 20 bytes

    # UnitFormatter params
    decimals: t.NotRequired[int]
    base: t.NotRequired[str]
    prefix: t.NotRequired[bool]

    # CalldataFormatter params
    callee_path: t.NotRequired[ERC7730Path]
    selector: t.NotRequired[str]  # hex (no 0x prefix), 4 bytes

    # enum fields: key->display mapping looked up on-device
    enum_values: t.NotRequired[list[ERC7730EnumValue]]


class ERC20DisplayFormat(t.TypedDict):
    chain_id: int
    address: str  # 0x-prefixed lowercase hex (20 bytes)
    func_sig: str  # 0x-prefixed lowercase hex (4 bytes)
    intent: str
    parameter_definitions: list[ABIValue]
    field_definitions: list[ERC7730Field]

    # metadata.owner, or "<registry subdir>: <contractName>" / subdir fallback
    provider_name: t.NotRequired[str]

    deleted: t.NotRequired[bool]
