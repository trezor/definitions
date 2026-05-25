"""ERC-7730 descriptor parser.

Walks a raw ERC-7730 calldata descriptor (post-`includes` merge) and produces
the JSON form used by `definitions-latest.json`. Function-signature parsing,
canonicalization, selector computation, path parsing, and `includes` merging
are delegated to the `erc7730` library.

We don't use the library's strict Pydantic descriptor model
(`InputERC7730Descriptor`) because the registry files in
`ethereum/clear-signing-erc7730-registry` use an older / looser v2 schema
that the library rejects (`legalName` missing, `visible` extra, `abi`
missing, etc.). So we keep our own dict-level descriptor walking.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from erc7730.common.abi import (
    compute_signature,
    parse_signature,
    signature_to_selector,
)
from erc7730.common.json import read_json_with_includes
from erc7730.model.abi import Component, Function
from erc7730.model.paths import (
    Array,
    ArrayElement,
    ArraySlice,
    ContainerField,
    ContainerPath,
    DataPath,
    DescriptorPath,
    Field as PathField,
)
from erc7730.model.paths.path_parser import to_path

from .common import (
    ABITuple,
    ABIValue,
    ERC20DisplayFormat,
    ERC7730Field,
    ERC7730Path,
)

LOG = logging.getLogger(__name__)


# =====================================================================
#                       Solidity type → ABI proto enum
# =====================================================================


_ABI_TYPE_MAP: dict[str, tuple[bool, str]] = {
    # (is_dynamic_when_unsized, EthereumABIType member name)
    "address": (False, "ABI_ADDRESS"),
    "bool": (False, "ABI_BOOL"),
    "bytes": (True, "ABI_BYTES"),
    "bytes4": (False, "ABI_BYTES4"),
    "bytes8": (False, "ABI_BYTES8"),
    "bytes16": (False, "ABI_BYTES16"),
    "bytes32": (False, "ABI_BYTES32"),
    "string": (True, "ABI_STRING"),
    "uint8": (False, "ABI_UINT8"),
    "uint16": (False, "ABI_UINT16"),
    "uint24": (False, "ABI_UINT24"),
    "uint32": (False, "ABI_UINT32"),
    "uint40": (False, "ABI_UINT40"),
    "uint48": (False, "ABI_UINT48"),
    "uint64": (False, "ABI_UINT64"),
    "uint72": (False, "ABI_UINT72"),
    "uint96": (False, "ABI_UINT96"),
    "uint112": (False, "ABI_UINT112"),
    "uint120": (False, "ABI_UINT120"),
    "uint128": (False, "ABI_UINT128"),
    "uint160": (False, "ABI_UINT160"),
    "uint248": (False, "ABI_UINT248"),
    "uint256": (False, "ABI_UINT256"),
}


def _split_array_suffix(type_str: str) -> tuple[str, int]:
    """Strip trailing `[]` pairs from a Solidity type. Returns (base, depth).

    Fixed-size arrays (`[N]`) are not supported and treated as the base type.
    """
    depth = 0
    while type_str.endswith("[]"):
        type_str = type_str[:-2]
        depth += 1
    return type_str, depth


def _component_is_dynamic(c: Component) -> bool:
    base, depth = _split_array_suffix(c.type)
    if depth != 0:
        return True
    if base == "tuple":
        return any(_component_is_dynamic(sub) for sub in (c.components or []))
    return _ABI_TYPE_MAP.get(base, (False, ""))[0]


def build_abi_value(c: Component) -> ABIValue:
    """Turn a `Component` into the nested-dict mirror of EthereumABIValueInfo."""
    base_type, array_depth = _split_array_suffix(c.type)

    base: ABIValue
    if base_type == "tuple":
        sub_components = c.components or []
        if any(
            (sub.type == "tuple" or sub.type.startswith("tuple"))
            and not sub.type.endswith("]")
            for sub in sub_components
        ):
            raise ValueError("nested tuples not supported")
        # When the tuple sits inside an array, the array layer carries dynamism;
        # the tuple element itself is treated as fixed for layout purposes.
        is_dynamic = (
            False
            if array_depth
            else any(_component_is_dynamic(sub) for sub in sub_components)
        )
        tup: ABITuple = {
            "fields": [build_abi_value(sub) for sub in sub_components],
            "is_dynamic": is_dynamic,
        }
        base = {"tuple": tup}
    else:
        if base_type not in _ABI_TYPE_MAP:
            raise ValueError(f"unknown ABI type: {c.type}")
        is_dynamic, enum_name = _ABI_TYPE_MAP[base_type]
        if is_dynamic:
            base = {"dynamic": enum_name}
        else:
            base = {"atomic": enum_name}

    wrapped: ABIValue = base
    for _ in range(array_depth):
        wrapped = {"array": wrapped}
    return wrapped


# =====================================================================
#                              Path resolution
# =====================================================================


_CONTAINER_MAP = {
    ContainerField.VALUE: "VALUE",
    ContainerField.FROM: "FROM",
    ContainerField.TO: "TO",
}


def path_to_dict(
    path_str: str, inputs: list[Component]
) -> ERC7730Path | None:
    """Convert an ERC-7730 path string to the proto path representation.

    Returns None for unsupported paths (caller should skip the field):
      * `$.metadata.constants.X` (descriptor paths)
      * `.[]` array iteration / slices
      * unknown name segments
    """
    try:
        parsed = to_path(path_str)
    except Exception as e:
        LOG.warning("path parse failed for %r: %s", path_str, e)
        return None

    if isinstance(parsed, ContainerPath):
        mapped = _CONTAINER_MAP.get(parsed.field)
        return None if mapped is None else {"container_path": mapped}

    if isinstance(parsed, DescriptorPath):
        # `$.metadata.constants.X` — constant lookup, not representable as a proto path.
        return None

    if not isinstance(parsed, DataPath):
        return None

    indices: list[int] = []
    current = inputs
    for element in parsed.elements:
        if isinstance(element, PathField):
            name_to_idx = {p.name: i for i, p in enumerate(current) if p.name}
            if element.identifier not in name_to_idx:
                return None
            i = name_to_idx[element.identifier]
            indices.append(i)
            sub_component = current[i]
            if sub_component.components:
                current = sub_component.components
        elif isinstance(element, ArrayElement):
            indices.append(element.index)
        elif isinstance(element, (Array, ArraySlice)):
            return None
        else:
            return None
    return {"path": indices}


# =====================================================================
#                              Field building
# =====================================================================


_FORMATTER_MAP = {
    "addressName": "FORMATTER_ADDRESS_NAME",
    "amount": "FORMATTER_AMOUNT",
    "tokenAmount": "FORMATTER_TOKEN_AMOUNT",
    "unit": "FORMATTER_UNIT",
}


def _normalize_hex(s: str) -> str:
    s = s.lower().removeprefix("0x")
    if len(s) % 2 == 1:
        s = "0" + s
    return s


def _resolve_constant(path_str: str, constants: dict[str, Any]) -> Any | None:
    """Resolve a `$.metadata.constants.<key>` path against `metadata.constants`.

    Returns the constant value, or None if the path isn't a constants lookup
    or the key is missing.
    """
    try:
        parsed = to_path(path_str)
    except Exception:
        return None
    if not isinstance(parsed, DescriptorPath):
        return None
    elements = parsed.elements
    if len(elements) != 3:
        return None
    e0, e1, e2 = elements
    if not (
        isinstance(e0, PathField)
        and isinstance(e1, PathField)
        and isinstance(e2, PathField)
    ):
        return None
    if e0.identifier != "metadata" or e1.identifier != "constants":
        return None
    return constants.get(e2.identifier)


def build_field_dict(
    field_def: dict[str, Any],
    inputs: list[Component],
    constants: dict[str, Any] | None = None,
    label_context: str = "",
) -> ERC7730Field | None:
    """Convert a single ERC-7730 field definition."""
    path_str = field_def.get("path")
    label = field_def.get("label", "")
    if not path_str:
        return None
    path = path_to_dict(str(path_str), inputs)
    if path is None:
        LOG.warning(
            "%s: skipping field %r — unsupported path %r",
            label_context,
            label,
            path_str,
        )
        return None

    fmt = field_def.get("format")
    if fmt is None:
        # `visible: never` fields, etc. — silently skip.
        return None
    if fmt not in _FORMATTER_MAP:
        LOG.warning(
            "%s: skipping field %r — unsupported formatter %r",
            label_context,
            label,
            fmt,
        )
        return None

    out: ERC7730Field = {
        "path": path,
        "label": label,
        "formatter": _FORMATTER_MAP[fmt],
    }

    constants = constants or {}
    params = field_def.get("params") or {}
    if fmt == "tokenAmount":
        token_path_str = params.get("tokenPath")
        if token_path_str:
            tp = path_to_dict(str(token_path_str), inputs)
            if tp is not None:
                out["token_path"] = tp
            else:
                LOG.warning(
                    "%s: dropping token_path for field %r — unsupported path %r",
                    label_context,
                    label,
                    token_path_str,
                )
        threshold = params.get("threshold")
        if isinstance(threshold, str) and threshold.startswith("$"):
            resolved = _resolve_constant(threshold, constants)
            if resolved is None:
                LOG.warning(
                    "%s: dropping threshold for field %r — unresolved %r",
                    label_context,
                    label,
                    threshold,
                )
                threshold = None
            else:
                threshold = resolved
        if isinstance(threshold, str):
            out["threshold"] = _normalize_hex(threshold)
        elif isinstance(threshold, int):
            out["threshold"] = _normalize_hex(hex(threshold))
    elif fmt == "unit":
        if params.get("decimals") is not None:
            out["decimals"] = int(params["decimals"])
        if params.get("base"):
            out["base"] = str(params["base"])
        if params.get("prefix") is not None:
            out["prefix"] = bool(params["prefix"])

    return out


def _get_intent(display_format: dict[str, Any]) -> str:
    intent = display_format.get("intent", "")
    if isinstance(intent, dict):
        return intent.get("en") or next(iter(intent.values()), "")
    return intent or ""


# =====================================================================
#                       Descriptor → display formats
# =====================================================================


def load_descriptor(path: Path) -> dict[str, Any]:
    """Load an ERC-7730 descriptor, recursively inlining any `includes`.

    Uses the `erc7730` library's merge: the calling file's keys win on
    conflict; `includes` may be a string or a list of sibling paths.
    """
    return read_json_with_includes(path)


def load_display_formats(path: Path) -> list[ERC20DisplayFormat]:
    """Convenience: `load_descriptor` + `build_display_formats`."""
    descriptor = load_descriptor(path)
    return build_display_formats(descriptor, source=path.name)


def build_display_formats(
    descriptor: dict[str, Any],
    source: str = "<descriptor>",
) -> list[ERC20DisplayFormat]:
    """Turn a single (post-includes) ERC-7730 descriptor into a list of records.

    Yields one record per (deployment × signature) pair.
    """
    context = descriptor.get("context") or {}
    contract = context.get("contract") or {}
    deployments = contract.get("deployments") or []
    formats = ((descriptor.get("display") or {}).get("formats")) or {}
    constants = (descriptor.get("metadata") or {}).get("constants") or {}
    if not deployments:
        LOG.info("%s: no deployments, skipping", source)
        return []

    results: list[ERC20DisplayFormat] = []

    for sig_key, display_format in formats.items():
        if sig_key.startswith("0x"):
            # Hex selector — we can't derive parameter types without an ABI,
            # so we can't build parameter_definitions. Skip.
            LOG.warning(
                "%s: skipping %s — selector-only entries are unsupported",
                source,
                sig_key,
            )
            continue
        try:
            parsed: Function = parse_signature(sig_key)
        except Exception as e:
            LOG.warning("%s: skipping %s — %s", source, sig_key, e)
            continue

        canonical = compute_signature(parsed)
        func_sig_hex = signature_to_selector(canonical)
        inputs = list(parsed.inputs or [])

        try:
            parameter_definitions = [build_abi_value(p) for p in inputs]
        except ValueError as e:
            LOG.warning("%s: skipping %s — %s", source, sig_key, e)
            continue

        field_defs: list[ERC7730Field] = []
        for f in display_format.get("fields", []):
            if not isinstance(f, dict) or "path" not in f:
                LOG.warning(
                    "%s: skipping non-field entry in %s: %r", source, sig_key, f
                )
                continue
            built = build_field_dict(
                f,
                inputs,
                constants=constants,
                label_context=f"{source}:{sig_key}",
            )
            if built is not None:
                field_defs.append(built)

        intent = _get_intent(display_format)

        for deployment in deployments:
            chain_id = deployment.get("chainId")
            address = deployment.get("address")
            if chain_id is None or not address:
                LOG.warning("%s: incomplete deployment %r", source, deployment)
                continue
            address_hex = "0x" + _normalize_hex(address)
            if len(address_hex) != 42:
                LOG.warning("%s: bad address length %s", source, address)
                continue
            results.append(
                {
                    "chain_id": int(chain_id),
                    "address": address_hex,
                    "func_sig": func_sig_hex,
                    "intent": intent,
                    "parameter_definitions": parameter_definitions,
                    "field_definitions": field_defs,
                }
            )

    return results
