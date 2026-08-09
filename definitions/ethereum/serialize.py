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
        EthereumNetworkInfo,
        EthereumTokenInfo,
    )
except ImportError as e:
    raise SystemExit(
        f"Import error: {e}\n\n"
        "Your trezorlib is outdated. Run:\n"
        "  uv pip install -e ../trezor-firmware/python\n"
        "  uv run --no-sync ./do_update.sh"
    ) from None


# A trezorlib that predates the ERC-7730 clear-signing proto additions
# imports fine but is missing them — which would otherwise surface only as a
# cryptic KeyError deep inside serialization. Fail fast with the same guidance.
_missing_proto = [
    f"EthereumERC7730FieldFormatterType.{name}"
    for name in ("FORMATTER_RAW", "FORMATTER_DATE")
    if not hasattr(EthereumERC7730FieldFormatterType, name)
]
if not any(
    f.name == "const_token_address" for f in EthereumERC7730FieldInfo.FIELDS.values()
):
    _missing_proto.append("EthereumERC7730FieldInfo.const_token_address")
if not any(f.name == "const_value" for f in EthereumERC7730Path.FIELDS.values()):
    _missing_proto.append("EthereumERC7730Path.const_value")
if not any(f.name == "slice_start" for f in EthereumERC7730Path.FIELDS.values()):
    _missing_proto.append("EthereumERC7730Path.slice_start")
if not hasattr(EthereumERC7730FieldFormatterType, "FORMATTER_CALLDATA"):
    _missing_proto.append("EthereumERC7730FieldFormatterType.FORMATTER_CALLDATA")
if not any(f.name == "callee_path" for f in EthereumERC7730FieldInfo.FIELDS.values()):
    _missing_proto.append("EthereumERC7730FieldInfo.callee_path")
if not hasattr(EthereumERC7730FieldFormatterType, "FORMATTER_ENUM"):
    _missing_proto.append("EthereumERC7730FieldFormatterType.FORMATTER_ENUM")
if not any(f.name == "enum_values" for f in EthereumERC7730FieldInfo.FIELDS.values()):
    _missing_proto.append("EthereumERC7730FieldInfo.enum_values")
if not any(
    f.name == "provider_name" for f in EthereumDisplayFormatInfo.FIELDS.values()
):
    _missing_proto.append("EthereumDisplayFormatInfo.provider_name")
if _missing_proto:
    raise SystemExit(
        "Your trezorlib is outdated — missing " + ", ".join(_missing_proto) + ".\n"
        "The ERC-7730 clear-signing formatters need the updated proto. Run:\n"
        "  uv pip install -e ../trezor-firmware/python\n"
        "  uv run --no-sync ./do_update.sh"
    )


def serialize_network(network: Network, timestamp: int) -> bytes:
    network_info = EthereumNetworkInfo(
        chain_id=network["chain_id"],
        symbol=network["shortcut"],
        slip44=network["slip44"],
        name=network["name"],
    )
    return encode_payload(network_info, DefinitionType.ETHEREUM_NETWORK, timestamp)


def serialize_token(token: ERC20Token, timestamp: int) -> bytes:
    token_info = EthereumTokenInfo(
        address=bytes.fromhex(token["address"][2:]),
        chain_id=token["chain_id"],
        symbol=token["shortcut"],
        decimals=token["decimals"],
        name=token["name"],
    )
    return encode_payload(token_info, DefinitionType.ETHEREUM_TOKEN, timestamp)


_ABI_VARIANT_KEYS = frozenset({"atomic", "dynamic", "tuple", "array"})
_PATH_VARIANT_KEYS = frozenset({"container_path", "path", "const_value"})


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
        return EthereumERC7730Path(
            path=list(d["path"]),
            slice_start=d.get("slice_start"),
            slice_end=d.get("slice_end"),
        )
    if "const_value" in d:
        return EthereumERC7730Path(const_value=d["const_value"])
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
        const_token_address=(
            bytes.fromhex(d["const_token_address"])
            if "const_token_address" in d
            else None
        ),
        callee_path=(
            _build_erc7730_path(d["callee_path"]) if "callee_path" in d else None
        ),
        selector=bytes.fromhex(d["selector"]) if "selector" in d else None,
        enum_values=(
            [
                EthereumERC7730EnumEntry(key=e["key"], value=e["value"])
                for e in d["enum_values"]
            ]
            if "enum_values" in d
            else None
        ),
    )


def _strip_0x(label: str, value: str) -> str:
    if not value.startswith("0x"):
        raise ValueError(f"{label} must start with '0x', got {value!r}")
    return value[2:]


def serialize_display_format(
    display_format: ERC20DisplayFormat, timestamp: int
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
    return encode_payload(info, DefinitionType.ETHEREUM_DISPLAY_FORMAT, timestamp)
