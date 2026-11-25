"""Serialization of Ethereum definitions into Trezor protobuf payloads."""

from __future__ import annotations

from ..common import encode_payload
from .types import (
    ABIValue,
    ERC20DisplayFormat,
    ERC20Token,
    ERC7730Field,
    ERC7730Path,
    Network,
    PathStep,
)

try:
    from trezorlib.messages import (
        DefinitionType,
        EthereumABITupleInfo,
        EthereumABIType,
        EthereumABIValueInfo,
        EthereumDisplayFormatInfo,
        EthereumERC7730ContainerPath,
        EthereumERC7730EnumEntry,
        EthereumERC7730FieldFormatterType,
        EthereumERC7730FieldInfo,
        EthereumERC7730Path,
        EthereumERC7730PathStep,
        EthereumNetworkInfo,
        EthereumTokenInfo,
    )
except ImportError as e:
    raise SystemExit(
        f"Import error: {e}\n\nYour trezorlib is outdated. Run:\n  uv sync"
    ) from None


# A trezorlib that predates the ERC-7730 clear-signing proto additions
# imports fine but is missing them — which would otherwise surface only as a
# cryptic KeyError deep inside serialization. Fail fast with the same guidance.
_missing_proto = [
    f"EthereumERC7730FieldFormatterType.{name}"
    for name in (
        "FORMATTER_RAW",
        "FORMATTER_DATE",
        "FORMATTER_DURATION",
        "FORMATTER_CALLDATA",
        "FORMATTER_ENUM",
        "FORMATTER_CHAIN_ID",
    )
    if not hasattr(EthereumERC7730FieldFormatterType, name)
]
if not any(
    f.name == "const_token_address" for f in EthereumERC7730FieldInfo.FIELDS.values()
):
    _missing_proto.append("EthereumERC7730FieldInfo.const_token_address")
if not any(f.name == "const_value" for f in EthereumERC7730Path.FIELDS.values()):
    _missing_proto.append("EthereumERC7730Path.const_value")
if not any(f.name == "steps" for f in EthereumERC7730Path.FIELDS.values()):
    _missing_proto.append("EthereumERC7730Path.steps")
if not any(f.name == "callee_path" for f in EthereumERC7730FieldInfo.FIELDS.values()):
    _missing_proto.append("EthereumERC7730FieldInfo.callee_path")
if not any(f.name == "enum_values" for f in EthereumERC7730FieldInfo.FIELDS.values()):
    _missing_proto.append("EthereumERC7730FieldInfo.enum_values")
if not any(f.name == "wide_key" for f in EthereumERC7730EnumEntry.FIELDS.values()):
    _missing_proto.append("EthereumERC7730EnumEntry.wide_key")
if not any(
    f.name == "lookup_address" for f in EthereumERC7730FieldInfo.FIELDS.values()
):
    _missing_proto.append("EthereumERC7730FieldInfo.lookup_address")
if not any(
    f.name == "threshold_message" for f in EthereumERC7730FieldInfo.FIELDS.values()
):
    _missing_proto.append("EthereumERC7730FieldInfo.threshold_message")
if not any(
    f.name == "native_currency_address"
    for f in EthereumERC7730FieldInfo.FIELDS.values()
):
    _missing_proto.append("EthereumERC7730FieldInfo.native_currency_address")
_missing_proto += [
    f"EthereumABIType.{name}"
    for name in (
        "ABI_INT24",
        "ABI_INT32",
        "ABI_INT64",
        "ABI_INT128",
        "ABI_INT160",
        "ABI_INT256",
        "ABI_UINT216",
    )
    if not hasattr(EthereumABIType, name)
]
for _name in (
    "if_not_in",
    "must_match",
    "chain_id_path",
    "const_chain_id",
    "selector_path",
    "amount_path",
    "const_amount",
    "spender_path",
    "const_callee",
    "const_spender",
):
    if not any(f.name == _name for f in EthereumERC7730FieldInfo.FIELDS.values()):
        _missing_proto.append(f"EthereumERC7730FieldInfo.{_name}")
if not hasattr(EthereumERC7730ContainerPath, "CHAIN_ID"):
    _missing_proto.append("EthereumERC7730ContainerPath.CHAIN_ID")
if not any(
    f.name == "provider_name" for f in EthereumDisplayFormatInfo.FIELDS.values()
):
    _missing_proto.append("EthereumDisplayFormatInfo.provider_name")
if _missing_proto:
    raise SystemExit(
        "Your trezorlib is outdated — missing " + ", ".join(_missing_proto) + ".\n"
        "The ERC-7730 clear-signing formatters need the updated proto. Run:\n"
        "  uv sync"
    )


def serialize_network(network: Network, timestamp: int, version: int) -> bytes:
    network_info = EthereumNetworkInfo(
        chain_id=network["chain_id"],
        symbol=network["shortcut"],
        slip44=network["slip44"],
        name=network["name"],
    )
    return encode_payload(
        network_info, DefinitionType.ETHEREUM_NETWORK, timestamp, version
    )


def serialize_token(token: ERC20Token, timestamp: int, version: int) -> bytes:
    token_info = EthereumTokenInfo(
        address=bytes.fromhex(token["address"][2:]),
        chain_id=token["chain_id"],
        symbol=token["shortcut"],
        decimals=token["decimals"],
        name=token["name"],
    )
    return encode_payload(token_info, DefinitionType.ETHEREUM_TOKEN, timestamp, version)


_ABI_VARIANT_KEYS = frozenset({"atomic", "dynamic", "tuple", "array"})
_PATH_VARIANT_KEYS = frozenset({"container_path", "steps", "const_value"})


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
    if "steps" in d:
        # Only `steps`: the deprecated `path` / `slice_*` are never set, so
        # firmware that predates steps sees an empty path and blind-signs.
        return EthereumERC7730Path(steps=[_build_path_step(s) for s in d["steps"]])
    if "const_value" in d:
        return EthereumERC7730Path(const_value=d["const_value"])
    raise AssertionError("unreachable")


def _build_path_step(s: PathStep) -> EthereumERC7730PathStep:
    is_slice = "slice_start" in s or "slice_end" in s
    if sum(("field" in s, "index" in s, "each" in s, is_slice)) != 1:
        raise ValueError(f"PathStep must have exactly one kind: {s}")
    return EthereumERC7730PathStep(**s)


def _build_erc7730_field_info(d: ERC7730Field) -> EthereumERC7730FieldInfo:
    return EthereumERC7730FieldInfo(
        path=_build_erc7730_path(d["path"]),
        label=d["label"],
        formatter=EthereumERC7730FieldFormatterType[d["formatter"]],
        token_path=(
            _build_erc7730_path(d["token_path"]) if "token_path" in d else None
        ),
        threshold=bytes.fromhex(d["threshold"]) if "threshold" in d else None,
        threshold_message=d.get("threshold_message"),
        decimals=d.get("decimals"),
        base=d.get("base"),
        prefix=d.get("prefix"),
        const_token_address=(
            bytes.fromhex(d["const_token_address"])
            if "const_token_address" in d
            else None
        ),
        native_currency_address=(
            [bytes.fromhex(a) for a in d["native_currency_address"]]
            if "native_currency_address" in d
            else None
        ),
        chain_id_path=(
            _build_erc7730_path(d["chain_id_path"]) if "chain_id_path" in d else None
        ),
        const_chain_id=d.get("const_chain_id"),
        callee_path=(
            _build_erc7730_path(d["callee_path"]) if "callee_path" in d else None
        ),
        const_callee=(
            bytes.fromhex(d["const_callee"]) if "const_callee" in d else None
        ),
        selector=bytes.fromhex(d["selector"]) if "selector" in d else None,
        selector_path=(
            _build_erc7730_path(d["selector_path"]) if "selector_path" in d else None
        ),
        amount_path=(
            _build_erc7730_path(d["amount_path"]) if "amount_path" in d else None
        ),
        const_amount=(
            bytes.fromhex(d["const_amount"]) if "const_amount" in d else None
        ),
        spender_path=(
            _build_erc7730_path(d["spender_path"]) if "spender_path" in d else None
        ),
        const_spender=(
            bytes.fromhex(d["const_spender"]) if "const_spender" in d else None
        ),
        enum_values=(
            [
                EthereumERC7730EnumEntry(
                    key=e["key"],
                    value=e["value"],
                    wide_key=(
                        bytes.fromhex(e["wide_key"]) if "wide_key" in e else None
                    ),
                )
                for e in d["enum_values"]
            ]
            if "enum_values" in d
            else None
        ),
        lookup_address=d.get("lookup_address"),
        if_not_in=(
            [bytes.fromhex(v) for v in d["if_not_in"]] if "if_not_in" in d else None
        ),
        must_match=(
            [bytes.fromhex(v) for v in d["must_match"]] if "must_match" in d else None
        ),
    )


def _strip_0x(label: str, value: str) -> str:
    if not value.startswith("0x"):
        raise ValueError(f"{label} must start with '0x', got {value!r}")
    return value[2:]


def serialize_display_format(
    display_format: ERC20DisplayFormat, timestamp: int, version: int
) -> bytes:
    info = EthereumDisplayFormatInfo(
        chain_id=display_format["chain_id"],
        provider_name=display_format.get("provider_name"),
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
    return encode_payload(
        info, DefinitionType.ETHEREUM_DISPLAY_FORMAT, timestamp, version
    )
