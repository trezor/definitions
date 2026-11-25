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
    container_path: str  # "FROM" | "VALUE" | "TO" | "CHAIN_ID"


class PathStep(t.TypedDict, total=False):
    """One EthereumERC7730PathStep: exactly one kind (a slice may set both bounds)."""

    field: int  # tuple member or top-level parameter
    index: int  # array element, negative = from the end
    each: bool  # `.[]`
    slice_start: int  # byte slice of the walked value (e.g. `token.[-20:]`)
    slice_end: int


class _DataPath(t.TypedDict):
    steps: list[PathStep]


class _ConstValuePath(t.TypedDict):
    const_value: str  # a literal constant value, not walked from calldata


ERC7730Path = _ContainerPath | _DataPath | _ConstValuePath


class ERC7730EnumValue(t.TypedDict):
    # the decoded calldata value (uint32); 0xFFFFFFFF is reserved for
    # "see wide_key"
    key: int
    value: str  # what the user sees for it
    # hex (no 0x prefix), minimal big-endian, at most 32 bytes: keys >= 0xFFFFFFFF
    wide_key: t.NotRequired[str]


class ERC7730Field(t.TypedDict):
    path: ERC7730Path
    label: str
    formatter: str  # e.g. "FORMATTER_ADDRESS_NAME"

    # TokenAmountFormatter params
    token_path: t.NotRequired[ERC7730Path]
    threshold: t.NotRequired[str]  # hex (no 0x prefix)
    threshold_message: t.NotRequired[str]
    const_token_address: t.NotRequired[str]  # hex (no 0x prefix), 20 bytes
    native_currency_address: t.NotRequired[
        list[str]
    ]  # hex (no 0x prefix), 20 bytes each

    # Chain the token lives on (tokenAmount / tokenTicker), mirroring
    # token_path / const_token_address. Absent: the transaction's chain.
    chain_id_path: t.NotRequired[ERC7730Path]
    const_chain_id: t.NotRequired[int]  # uint64

    # UnitFormatter params
    decimals: t.NotRequired[int]
    base: t.NotRequired[str]
    prefix: t.NotRequired[bool]

    # CalldataFormatter params
    callee_path: t.NotRequired[ERC7730Path]
    const_callee: t.NotRequired[str]  # hex (no 0x prefix), 20 bytes
    selector: t.NotRequired[str]  # hex (no 0x prefix), 4 bytes
    selector_path: t.NotRequired[ERC7730Path]  # bytes4 leaf or 4-byte slice
    amount_path: t.NotRequired[ERC7730Path]  # the inner call's @.value
    # the inner call's @.value: hex (no 0x prefix), minimal big-endian uint256
    const_amount: t.NotRequired[str]
    spender_path: t.NotRequired[ERC7730Path]  # the inner call's @.from
    const_spender: t.NotRequired[str]  # hex (no 0x prefix), 20 bytes

    # enum fields: key->display mapping looked up on-device
    enum_values: t.NotRequired[list[ERC7730EnumValue]]

    # AddressNameFormatter params
    # True when the descriptor's `types` include `contract` or `token`: the
    # device asks the host for a known name for the address.
    lookup_address: t.NotRequired[bool]

    # Conditional visibility: hex (no 0x prefix) encodings of the listed
    # values, typed by the field's leaf (see erc7730.py section 4c).
    # if_not_in: show the field only when its value is not listed.
    # must_match: cancel signing; the value must be one of those listed.
    if_not_in: t.NotRequired[list[str]]
    must_match: t.NotRequired[list[str]]

    # Iterating fields of one `iteration: bundled` group share an id (unique
    # within the display format): element i of each is shown together.
    bundle_id: t.NotRequired[int]  # uint32


class ERC20DisplayFormat(t.TypedDict):
    chain_id: int
    address: str  # 0x-prefixed lowercase hex (20 bytes)
    func_sig: str  # 0x-prefixed lowercase hex (4 bytes, 8 for name-only records)
    intent: str
    parameter_definitions: list[ABIValue]
    field_definitions: list[ERC7730Field]

    # metadata.owner, or "<registry subdir>: <contractName>" / subdir fallback
    provider_name: t.NotRequired[str]

    deleted: t.NotRequired[bool]
