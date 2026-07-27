"""ERC-7730 descriptor parser.

Reads calldata descriptors from the clear-signing registry and produces the
`erc20_display_formats` records of `definitions-latest.json`, which later
serialize into the firmware's `EthereumDisplayFormatInfo` protobuf.

Pipeline, one registry JSON file at a time::

    load_display_formats(path)
      load_descriptor()             resolve `includes` into one plain dict
      build_display_formats()       for each function signature in the file:
        _build_one_format()
          parse_signature()           (erc7730 library)
          build_abi_value()           Solidity type -> ABI type tree  [section 1]
          _build_path_field() /       one proto field per displayed
          _build_non_path_field()     descriptor field                [section 4]
            path_to_dict()            ERC-7730 path -> proto path     [section 2]
      then one output record per (signature x deployment)             [section 5]

A descriptor field the firmware cannot faithfully render has one of two
outcomes, and the distinction runs through the whole module:

  * DROP -- `UnsupportedFeature(feature, detail)` is raised and the whole
    display format (that one signature) is dropped: we never emit a format
    with a field silently missing. Other signatures in the same file still go
    through. Drops are collected into the caller's `unsupported` list, keyed
    by a stable feature tag for the log.

  * ADJUST -- the field is kept, but deliberately bent into something the
    firmware can render: a formatter override, an ABI leaf retype, a constant
    materialized as a literal string. Every bend is collected into the
    caller's `adjustments` list so it is visible in `definitions-latest.log`;
    nothing is changed silently.

Everything here is dict-level walking. The registry still uses an older /
looser v2 schema that the `erc7730` library's strict Pydantic model rejects
(`legalName` missing, `visible` extra, `abi` missing, ...), so we only borrow
the library's signature/path parsing, not its descriptor model.
"""

from __future__ import annotations

import dataclasses
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


class UnsupportedFeature(Exception):
    """A *displayed* field uses a feature we can't faithfully represent.

    Raising this drops the display format being built (the DROP outcome in the
    module docstring). `feature` is a stable tag the log aggregates on;
    `detail` is the human-readable specifics.
    """

    def __init__(self, feature: str, detail: str):
        self.feature = feature
        self.detail = detail
        super().__init__(f"{feature}: {detail}")


# =====================================================================
# 1. Solidity types -> ABI type trees
#
# A function signature's parameter types become a list of ABIValue dicts
# (`parameter_definitions`), the exact mirror of the firmware's
# EthereumABIValueInfo proto: {"atomic": <enum>}, {"dynamic": <enum>},
# {"tuple": {...}} or {"array": <ABIValue>} nested per array dimension.
# The firmware walks this tree to decode raw calldata.
# =====================================================================


_ABI_TYPE_MAP: dict[str, tuple[bool, str]] = {
    # Solidity base type -> (is dynamically sized, EthereumABIType member)
    # Mirrors exactly what the firmware's `_get_parser` accepts; a type
    # missing here (e.g. other intN widths) is a firmware gap, not ours.
    "address": (False, "ABI_ADDRESS"),
    "bool": (False, "ABI_BOOL"),
    "bytes": (True, "ABI_BYTES"),
    "bytes4": (False, "ABI_BYTES4"),
    "bytes8": (False, "ABI_BYTES8"),
    "bytes16": (False, "ABI_BYTES16"),
    "bytes20": (False, "ABI_BYTES20"),
    "bytes32": (False, "ABI_BYTES32"),
    "int160": (False, "ABI_INT160"),
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

    Fixed-size arrays (`[N]`) cannot be represented in our ABI value model
    (the proto carries no element count), so any fixed dimension raises.
    """
    original = type_str
    depth = 0
    while type_str.endswith("[]"):
        type_str = type_str[:-2]
        depth += 1
    # Any remaining `[` is a fixed-size dimension, e.g. `uint256[2]`,
    # `uint256[2][]`, or `uint256[][2]`.
    if "[" in type_str:
        raise UnsupportedFeature(
            "fixed-size-array",
            f"{original} (fixed-size arrays are not supported)",
        )
    return type_str, depth


def _component_is_dynamic(c: Component) -> bool:
    base, depth = _split_array_suffix(c.type)
    if depth != 0:
        return True
    if base == "tuple":
        return any(_component_is_dynamic(sub) for sub in (c.components or []))
    return _ABI_TYPE_MAP.get(base, (False, ""))[0]


def build_abi_value(c: Component) -> ABIValue:
    """Turn one signature parameter into its ABIValue tree.

    The shape restrictions below all mirror hard limits of the firmware's
    decoder (`ABIValue.from_proto` / `_get_leaf_parser` in clear_signing.py);
    emitting a shape the firmware would reject or, worse, mis-decode is a DROP.
    """
    base_type, array_depth = _split_array_suffix(c.type)

    base: ABIValue
    if base_type == "tuple":
        # The firmware models a tuple nested in at most ONE array layer;
        # `tuple[][]` raises `InvalidFormatDefinition` on-device.
        if array_depth >= 2:
            raise UnsupportedFeature(
                "tuple-in-nested-array",
                f"{c.type} (a tuple may be nested in at most one array)",
            )
        sub_components = c.components or []
        # The firmware decodes every tuple field as an atomic/dynamic *leaf*,
        # so a tuple field that is itself a tuple or an array can't be
        # represented. A leaf never starts with `tuple` nor ends with `]`.
        non_leaf = next(
            (
                sub.type
                for sub in sub_components
                if sub.type.startswith("tuple") or sub.type.endswith("]")
            ),
            None,
        )
        if non_leaf is not None:
            raise UnsupportedFeature(
                "non-leaf-tuple-field",
                f"{c.type} (tuple field of type {non_leaf!r}; tuple fields must be "
                f"atomic or dynamic leaves)",
            )
        tuple_is_dynamic = any(_component_is_dynamic(sub) for sub in sub_components)
        if array_depth and not tuple_is_dynamic:
            # The firmware decodes an array's tuple elements via an offset
            # table, which is how *dynamic* tuples are ABI-encoded. A static
            # tuple inside an array is encoded inline with a fixed stride and
            # no offsets, so the firmware would misread it — refuse to emit
            # rather than ship a wrong decode.
            raise UnsupportedFeature(
                "static-tuple-in-array",
                f"{c.type} (static tuples inside arrays are not supported)",
            )
        # In an array, the array layer carries the dynamism and the firmware
        # always parses the in-array tuple as static; only a top-level tuple
        # carries its own dynamism flag.
        tup: ABITuple = {
            "fields": [build_abi_value(sub) for sub in sub_components],
            "is_dynamic": tuple_is_dynamic and not array_depth,
        }
        base = {"tuple": tup}
    else:
        if base_type not in _ABI_TYPE_MAP:
            raise ValueError(f"unknown ABI type: {c.type}")
        # The firmware models an atomic/dynamic leaf nested in at most TWO
        # array layers; `T[][][]` raises `InvalidFormatDefinition` on-device.
        if array_depth >= 3:
            raise UnsupportedFeature(
                "array-nesting-too-deep",
                f"{c.type} (arrays may be nested at most two deep)",
            )
        is_dynamic, enum_name = _ABI_TYPE_MAP[base_type]
        base = {"dynamic": enum_name} if is_dynamic else {"atomic": enum_name}

    wrapped: ABIValue = base
    for _ in range(array_depth):
        wrapped = {"array": wrapped}
    return wrapped


# =====================================================================
# 2. ERC-7730 paths -> proto paths
#
# A descriptor field points at the value it displays with a path string:
#   "params.amountIn"  into calldata (relative to the signature's inputs),
#   "@.value"          a transaction-container field,
#   "$.metadata.…"     a descriptor-internal reference (handled in section 3).
# We turn a calldata path into a flat list of integer indices into the ABI
# type tree of section 1 — the firmware walks the decoded values with the
# same indices. Alongside the proto path we return the *kind* of leaf the
# path lands on, so section 4 can check it against the field's formatter.
#
# A path may end in a byte slice (`token.[-20:]`, `goodUntil.[-4:]`): the
# firmware views the sliced word as 32 big-endian bytes and takes the slice
# (`_word_bytes` in clear_signing.py), carried in the proto as the optional
# `slice_start` / `slice_end` fields next to the index list. Descriptors use
# this to unpack values crammed into one word — e.g. 1inch's `Address` type,
# a uint256 whose low 20 bytes are an address.
# =====================================================================


_CONTAINER_MAP = {
    ContainerField.VALUE: "VALUE",
    ContainerField.FROM: "FROM",
    ContainerField.TO: "TO",
}

# Leaf-value kinds used for formatter <-> type compatibility checks.
KIND_ADDRESS = "address"
KIND_NUMERIC = "numeric"  # any uint* / int* — decodes to an int on-device
KIND_BYTES = "bytes"  # bool / bytesN / bytes / string — only `raw` renders these
KIND_OTHER = "other"  # un-indexed array, tuple, or unknown — not one field's value


def _classify_kind(base_type: str, array_depth: int) -> str:
    """Classify a resolved leaf Solidity type into a formatter-compat kind."""
    if array_depth > 0:
        # Still an array (e.g. an un-indexed `uint256[]`): no scalar formatter
        # renders it (a `.[]` iteration peels the dimension first).
        return KIND_OTHER
    if base_type == "address":
        return KIND_ADDRESS
    if base_type.startswith("uint") or base_type.startswith("int"):
        return KIND_NUMERIC
    if base_type == "bool" or base_type == "string" or base_type.startswith("bytes"):
        # A scalar bool / bytesN / bytes / string leaf — the firmware's
        # RawFormatter renders these (bytes as hex, bool as text, string
        # as-is), but no other formatter does.
        return KIND_BYTES
    # tuple / unknown — not representable as a single rendered value.
    return KIND_OTHER


def _nominal_slice_length(start: int | None, end: int | None) -> int | None:
    """Byte length a `[start:end]` slice selects, when statically known.

    `[-20:]` is 20 bytes, `[:1]`/`[0:1]` is 1, `[0:20]` is 20. Mixed-sign
    bounds (`[-20:32]`) depend on the runtime value's length -> None.
    """
    if end is None:
        return -start if (start is not None and start < 0) else None
    s = start or 0
    if (s >= 0) == (end >= 0):
        n = end - s
        return n if n > 0 else None
    return None


def path_to_dict(path_str: str, inputs: list[Component]) -> tuple[ERC7730Path, str]:
    """Convert an ERC-7730 path string to `(proto path, leaf kind)`.

    A trailing `.[]` (whole-array iteration) is supported: the proto path
    points at the array itself and the firmware formats each element, so the
    returned kind is the *element* kind (`amounts.[]` over `uint256[]` is
    numeric).

    A trailing byte slice on a scalar leaf is supported (see the section
    comment): the bounds ride in the proto's `slice_start` / `slice_end` and
    the sliced value is bytes on-device. The returned kind is KIND_ADDRESS
    when the slice statically selects 20 bytes (`token.[-20:]` — the packed
    address pattern), else KIND_BYTES.

    Every unsupported path raises `UnsupportedFeature` with a distinct feature
    tag, so the drop reason is visible in the log:
      * `descriptor-path` — `$.…` paths (callers resolve constants *before*
        calling; one reaching this function is not a constants lookup we handle)
      * `per-element-field-path` — anything following a `.[]`
        (e.g. `swaps.[].amount`), which can't be expressed as a flat index path
      * `array-slice-path` — a slice we can't represent: applied to an array
        or tuple rather than a scalar word, or with out-of-range bounds
      * `non-trailing-slice` — path elements after a slice (the proto carries
        exactly one trailing slice)
      * `iteration-over-non-array` — a `.[]` applied to a non-array leaf
      * `unknown-path-segment` — a name not present in the ABI signature
      * `unparseable-path` — the path string didn't parse at all
      * `unsupported-container-path` — anything under `@.` other than
        `value` / `from` / `to` (transaction-level, security-relevant fields)
      * `fixed-size-array` (via `_split_array_suffix`)
    """
    try:
        parsed = to_path(path_str)
    except Exception as e:
        if path_str.strip().startswith("@"):
            raise UnsupportedFeature(
                "unsupported-container-path",
                f"{path_str} (only @.value / @.from / @.to are supported)",
            ) from e
        raise UnsupportedFeature("unparseable-path", f"{path_str}: {e}") from e

    if isinstance(parsed, ContainerPath):
        mapped = _CONTAINER_MAP.get(parsed.field)
        if mapped is None:
            # Unreachable today (ContainerField is value/from/to), but guards
            # against a future library adding a container field we don't map.
            raise UnsupportedFeature(
                "unsupported-container-path",
                f"{path_str} (unmapped container field {parsed.field!r})",
            )
        # `@.value` is the native amount (wei); `@.from` / `@.to` are addresses.
        kind = KIND_NUMERIC if mapped == "VALUE" else KIND_ADDRESS
        return {"container_path": mapped}, kind

    if isinstance(parsed, DescriptorPath):
        raise UnsupportedFeature("descriptor-path", path_str)

    if not isinstance(parsed, DataPath):
        raise UnsupportedFeature("unparseable-path", f"{path_str}: not a data path")

    # Walk the signature's components along the path, translating each name to
    # its positional index and tracking the Solidity type of the current leaf.
    indices: list[int] = []
    current = inputs
    leaf_base: str | None = None
    leaf_array_depth = 0
    saw_array_iter = False
    slice_bounds: tuple[int | None, int | None] | None = None
    for element in parsed.elements:
        if slice_bounds is not None:
            # The proto carries exactly one slice, applied after all indices —
            # so a slice must be the path's final element.
            raise UnsupportedFeature("non-trailing-slice", path_str)
        if saw_array_iter:
            # `.[]` resolves the path to the whole array, formatted element by
            # element — nothing may follow it. A per-element field extraction
            # (`swaps.[].amount`) has no flat-index representation, and a
            # per-element slice (`xs.[].[-20:]`) would be misapplied by the
            # firmware to the array itself rather than to each element.
            raise UnsupportedFeature("per-element-field-path", path_str)
        if isinstance(element, PathField):
            name_to_idx = {p.name: i for i, p in enumerate(current) if p.name}
            if element.identifier not in name_to_idx:
                raise UnsupportedFeature(
                    "unknown-path-segment",
                    f"{path_str} (segment {element.identifier!r})",
                )
            i = name_to_idx[element.identifier]
            indices.append(i)
            sub_component = current[i]
            leaf_base, leaf_array_depth = _split_array_suffix(sub_component.type)
            if sub_component.components:
                current = sub_component.components
        elif isinstance(element, ArrayElement):
            indices.append(element.index)
            if leaf_array_depth > 0:
                leaf_array_depth -= 1  # indexing peels one array dimension
        elif isinstance(element, Array):
            # Trailing `.[]`: point at the array itself (no index appended) and
            # peel one dimension so the kind reflects the per-element type. A
            # leaf that is *still* an array afterwards (`uint256[][]`)
            # classifies as KIND_OTHER — only flat scalar arrays iterate.
            if leaf_array_depth <= 0:
                raise UnsupportedFeature("iteration-over-non-array", path_str)
            leaf_array_depth -= 1
            saw_array_iter = True
        elif isinstance(element, ArraySlice):
            # A byte slice of a scalar word (`token.[-20:]`): the firmware
            # views the value as big-endian bytes and slices those. Slicing an
            # array or tuple would select *elements* instead — a different
            # feature we don't emit.
            leaf_kind = (
                KIND_OTHER
                if leaf_base is None
                else _classify_kind(leaf_base, leaf_array_depth)
            )
            if leaf_kind == KIND_OTHER:
                raise UnsupportedFeature(
                    "array-slice-path", f"{path_str} (slice of a non-scalar value)"
                )
            start, end = element.start, element.end
            if start is None and end is None:
                raise UnsupportedFeature("unparseable-path", f"{path_str}: empty slice")
            for bound in (start, end):
                # The proto fields are sint32.
                if bound is not None and not -(2**31) <= bound < 2**31:
                    raise UnsupportedFeature(
                        "array-slice-path",
                        f"{path_str} (slice bound {bound} out of sint32 range)",
                    )
            slice_bounds = (start, end)
        else:
            raise UnsupportedFeature(
                "unparseable-path", f"{path_str}: unhandled element {element!r}"
            )

    out: ERC7730Path = {"path": indices}
    if slice_bounds is not None:
        start, end = slice_bounds
        if start is not None:
            out["slice_start"] = start
        if end is not None:
            out["slice_end"] = end
        # The sliced value is bytes on-device. A statically 20-byte slice is
        # the packed-address pattern and renders/behaves as an address.
        kind = (
            KIND_ADDRESS if _nominal_slice_length(start, end) == 20 else KIND_BYTES
        )
        return out, kind

    kind = KIND_OTHER if leaf_base is None else _classify_kind(leaf_base, leaf_array_depth)
    return out, kind


# =====================================================================
# 3. Descriptor-internal references
#
# Two `$.` reference families appear in field definitions:
#   $.metadata.constants.<key>   -> a literal value from metadata.constants
#   $.display.definitions.<key>  -> a shared field definition to merge in
# =====================================================================


_HEX_DIGITS = frozenset("0123456789abcdef")


def _normalize_hex(s: str) -> str:
    s = s.lower().removeprefix("0x")
    if len(s) % 2 == 1:
        s = "0" + s
    return s


def _is_hex(s: str) -> bool:
    """Whether `s` is a non-empty string of hex digits (no `0x` prefix)."""
    if not s:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


def _descriptor_path_parts(path_str: str) -> tuple[str, str, str] | None:
    """Parse a `$.a.b.c` descriptor path into `(a, b, c)`, else None."""
    try:
        parsed = to_path(path_str)
    except Exception:
        return None
    if not isinstance(parsed, DescriptorPath) or len(parsed.elements) != 3:
        return None
    if not all(isinstance(e, PathField) for e in parsed.elements):
        return None
    e0, e1, e2 = parsed.elements
    return e0.identifier, e1.identifier, e2.identifier


def _resolve_constant(path_str: str, constants: dict[str, Any]) -> Any | None:
    """Resolve a `$.metadata.constants.<key>` path against `metadata.constants`.

    Returns the constant value, or None if the path isn't a constants lookup
    or the key is missing.
    """
    parts = _descriptor_path_parts(path_str)
    if parts is None or parts[:2] != ("metadata", "constants"):
        return None
    return constants.get(parts[2])


def _resolve_address_ref(value: Any, constants: dict[str, Any]) -> str | None:
    """Resolve a token-address reference to normalized 20-byte hex (no `0x`).

    `value` is a literal address string or a `$.metadata.constants.*`
    reference. Returns None if it can't be resolved to a valid 20-byte address.
    """
    s = str(value)
    if s.startswith("$"):
        resolved = _resolve_constant(s, constants)
        if resolved is None:
            return None
        s = str(resolved)
    s = _normalize_hex(s)
    if len(s) != 40 or set(s) - _HEX_DIGITS:
        return None
    return s


def _native_currency_includes_zero(
    params: dict[str, Any], constants: dict[str, Any]
) -> bool:
    """Whether `nativeCurrencyAddress` lists the zero address.

    A `tokenAmount` with no `tokenPath`/`token` has a null (zero-address)
    token. When the descriptor declares the zero address as a native-currency
    sentinel, that null token *is* the chain's native currency, so the amount
    is native. Entries may be literals or `$.metadata.constants.*` references.
    """
    raw = params.get("nativeCurrencyAddress")
    if raw is None:
        return False
    for entry in raw if isinstance(raw, list) else [raw]:
        s = str(entry)
        if s.startswith("$"):
            resolved = _resolve_constant(s, constants)
            if resolved is None:
                continue
            s = str(resolved)
        try:
            if int(_normalize_hex(s), 16) == 0:
                return True
        except ValueError:
            continue
    return False


def _resolve_ref(
    field_def: dict[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any] | None:
    """Merge a `$.display.definitions.*` reference into a field dict.

    Field-level keys override the definition; `params` dicts are deep-merged
    so the field can add keys (e.g. `tokenPath`) without losing definition
    keys (e.g. `nativeCurrencyAddress`).

    Returns None for an unresolvable `$ref` (not a `$.display.definitions.*`
    path, or a missing definition). The field's display info would come from
    that definition, so leaving the ref unmerged risks silently dropping a
    displayed field; the caller must drop the display format instead.
    """
    ref = field_def.get("$ref")
    if ref is None:
        return field_def

    parts = _descriptor_path_parts(str(ref))
    if parts is None or parts[:2] != ("display", "definitions"):
        LOG.warning("unsupported $ref path: %r", ref)
        return None

    definition = definitions.get(parts[2])
    if definition is None:
        LOG.warning("$ref definition not found: %r", parts[2])
        return None

    merged: dict[str, Any] = {**definition}
    for key, value in field_def.items():
        if key == "$ref":
            continue
        if key == "params" and isinstance(value, dict) and isinstance(merged.get("params"), dict):
            merged["params"] = {**merged["params"], **value}
        else:
            merged[key] = value

    return merged


# =====================================================================
# 4. Building one display field
#
# The entry points are `_build_path_field` (field bound to calldata) and
# `_build_non_path_field` (field bound to a constant). Both take the shared
# `_FormatContext` and either return a proto-field dict, return None (hidden
# field), or raise `UnsupportedFeature` (DROP).
# =====================================================================


_FORMATTER_MAP = {
    "addressName": "FORMATTER_ADDRESS_NAME",
    "amount": "FORMATTER_AMOUNT",
    "tokenAmount": "FORMATTER_TOKEN_AMOUNT",
    "unit": "FORMATTER_UNIT",
    # The firmware renders `raw` per Solidity type (int as decimal,
    # address/bytes as hex, bool as text, string as-is) and `date` as a
    # human-readable unix timestamp.
    "raw": "FORMATTER_RAW",
    "date": "FORMATTER_DATE",
    # `calldata` is the embedded calldata of a nested contract call. The
    # firmware resolves the callee address from `callee_path`, fetches that
    # contract's display format, and renders the nested call as extra rows
    # (CalldataFormatter in clear_signing.py).
    "calldata": "FORMATTER_CALLDATA",
    # `enum` looks the decoded calldata value up in the descriptor-supplied
    # `enum_values` mapping and shows the mapped string; a key missing from
    # the mapping fails clear signing on-device (blind-signing fallback)
    # rather than risk showing a wrong value.
    "enum": "FORMATTER_ENUM",
}

# The leaf-value kind(s) each formatter accepts. `_check_kind_or_reinterpret`
# enforces this; `addressName` gets reinterpretations instead of a hard drop.
_FORMATTER_VALUE_KIND = {
    "addressName": frozenset({KIND_ADDRESS}),
    "amount": frozenset({KIND_NUMERIC}),
    "tokenAmount": frozenset({KIND_NUMERIC}),
    "unit": frozenset({KIND_NUMERIC}),
    # `raw` renders any scalar leaf; only whole arrays / tuples are rejected.
    "raw": frozenset({KIND_ADDRESS, KIND_NUMERIC, KIND_BYTES}),
    # `date` paths point at a uint timestamp/blockheight.
    "date": frozenset({KIND_NUMERIC}),
    # embedded calldata is a dynamic `bytes` value.
    "calldata": frozenset({KIND_BYTES}),
    # enum keys are small uints; KIND_BYTES admits the bool case
    # (True/False keys over a bool value, mapped to 1/0).
    "enum": frozenset({KIND_NUMERIC, KIND_BYTES}),
}


@dataclasses.dataclass
class _FormatContext:
    """State shared while building the fields of ONE display format.

    `parameter_definitions` is the ABI type tree (section 1) that the fields'
    paths index into; reinterpretations mutate it in place. `adjustments`
    collects the (kind, detail) record of every accepted-but-bent field — the
    caller throws it away if the display format ends up dropped, so only
    adjustments of *emitted* formats are reported.
    """

    inputs: list[Component]
    constants: dict[str, Any]
    parameter_definitions: list[ABIValue]
    enums: dict[str, Any] = dataclasses.field(default_factory=dict)
    adjustments: list[tuple[str, str]] = dataclasses.field(default_factory=list)

    def adjust(self, kind: str, detail: str) -> None:
        LOG.info("adjustment %s: %s", kind, detail)
        self.adjustments.append((kind, detail))


def _field_is_displayed(
    field_def: dict[str, Any], ctx: _FormatContext | None = None
) -> bool:
    """Whether a field is meant to be shown to the user.

    Not-displayed fields carry no display information, so we skip them without
    validating their path/formatter/type — they are never "missing fields".
    A field is hidden when it has no `format`, or `visible` is `never`/`false`.

    `visible: optional` means "wallets MAY display this field if possible or
    sensible" (ERC-7730) — hiding it is spec-compliant, so it is skipped like
    `never`, logged as an adjustment.

    The *rule objects* (`ifNotIn`, `mustMatch`) are true conditionals the
    firmware cannot honor either way — `mustMatch` even carries a validation
    duty — so any such value raises and drops the display format.
    """
    if field_def.get("format") is None:
        return False
    visible = field_def.get("visible")
    if visible in (False, "never"):
        return False
    if visible in (None, True, "always"):
        return True
    if visible == "optional":
        if ctx is not None:
            ctx.adjust(
                "optional-field-hidden",
                f"visible=optional — hidden (label {field_def.get('label')!r})",
            )
        return False
    raise UnsupportedFeature(
        "conditional-visibility",
        f"visible={visible!r} (label {field_def.get('label')!r})",
    )


# ---------------------------------------------------------------------
# 4a. Constant (non-path) fields
# ---------------------------------------------------------------------


def _build_non_path_field(
    field_def: dict[str, Any], ctx: _FormatContext
) -> ERC7730Field | None:
    """Build a displayed field that has no calldata `path`.

    The only representable shape is a constant: `format: raw` with a `value`
    (a literal or a `$.metadata.constants.*` reference), e.g. a vault's share
    ticker. Hidden fields return None; anything else is a DROP.
    """
    if not _field_is_displayed(field_def, ctx):
        return None
    if "value" not in field_def:
        raise UnsupportedFeature(
            "non-path-field",
            f"{field_def.get('format')} (label {field_def.get('label')!r})",
        )
    label = field_def.get("label", "")
    if not label:
        raise UnsupportedFeature(
            "missing-label", f"constant field {field_def.get('value')!r}"
        )
    return _const_value_field(label, field_def["format"], field_def["value"], ctx)


def _const_value_field(
    label: str,
    fmt: str,
    value: Any,
    ctx: _FormatContext,
    *,
    already_resolved: bool = False,
) -> ERC7730Field:
    """Build a field bound to a constant value instead of a calldata path.

    The value rides in the proto as a `const_value` path, which the firmware
    renders as-is via the raw formatter — hence only `format: raw` is
    representable (every other formatter needs a typed calldata value).
    """
    if not already_resolved and isinstance(value, str) and value.startswith("$"):
        resolved = _resolve_constant(value, ctx.constants)
        if resolved is None:
            raise UnsupportedFeature(
                "unresolvable-constant-value", f"{value!r} (field {label!r})"
            )
        value = resolved

    if fmt != "raw":
        raise UnsupportedFeature(
            "constant-value-formatter",
            f"{fmt} with a constant value (field {label!r}); only raw is supported",
        )

    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        rendered = str(value)
    else:
        raise UnsupportedFeature(
            "invalid-constant-value", f"{value!r} (field {label!r})"
        )
    if not isinstance(value, str):
        ctx.adjust(
            "constant-value-stringified",
            f"{value!r} rendered as {rendered!r} (field {label!r})",
        )
    ctx.adjust(
        "constant-value-field", f"field {label!r} bound to constant {rendered!r}"
    )
    return {
        "path": {"const_value": rendered},
        "label": label,
        "formatter": _FORMATTER_MAP["raw"],
    }


# ---------------------------------------------------------------------
# 4b. Formatter/type compatibility and reinterpretations
#
# Registry authors routinely declare a parameter as `uint256` when the value
# is semantically an address: 1inch's `uniswapV3Swap(..., uint256[] pools)`
# holds pool addresses in the words' low 160 bits, maker orders put the
# receiver in a uint, packed token addresses appear in `tokenPath` targets.
# The firmware's AddressNameFormatter renders `bytes` and raises on `int`, so
# emitting FORMATTER_ADDRESS_NAME over a uint leaf as-is would fail on-device.
#
# The one lever we control is the declared ABI type in parameter_definitions.
# `uintN` and `address` are encoded identically — one static 32-byte word —
# so flipping the leaf's type changes nothing about WHERE anything decodes,
# only how the firmware interprets and renders that word (`parse_address`
# takes the low 20 bytes and requires the high 12 to be zero; a "dirty" value
# raises on-device, which degrades safely to blind signing).
# ---------------------------------------------------------------------


def _check_kind_or_reinterpret(
    fmt: str,
    path: ERC7730Path,
    kind: str,
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Verify the formatter can render the leaf the path points at.

    A mismatch is normally a DROP. `addressName` gets two reinterpretations
    (see the section comment above), both logged as adjustments:

      * on a uint leaf, the ABI leaf is retyped to `address` in place;
      * on a bytes-like leaf, the field passes through unchanged — the
        firmware's AddressNameFormatter accepts bytes/str and renders hex.

    One exact (non-adjustment) allowance: `date` over a byte slice
    (`goodUntil.[-4:]`) — the firmware's DateFormatter converts the sliced
    big-endian bytes to the integer timestamp.
    """
    if kind in _FORMATTER_VALUE_KIND[fmt]:
        return

    if fmt == "date" and kind == KIND_BYTES and ("slice_start" in path or "slice_end" in path):
        return

    if fmt == "addressName" and kind == KIND_NUMERIC and "path" in path:
        if _retype_uint_leaf_as_address(ctx.parameter_definitions, path["path"]):
            ctx.adjust(
                "address-in-numeric",
                f"{path_str} is numeric but formatted as addressName — "
                f"ABI leaf retyped to address (field {label!r})",
            )
            return
    if fmt == "addressName" and kind == KIND_BYTES:
        ctx.adjust(
            "addressname-on-bytes",
            f"{path_str} is {kind} but formatted as addressName — "
            f"rendered as hex (field {label!r})",
        )
        return

    raise UnsupportedFeature(
        "formatter-type-mismatch",
        f"{fmt} expects {'/'.join(sorted(_FORMATTER_VALUE_KIND[fmt]))} but "
        f"{path_str!r} is {kind} (field {label!r})",
    )


def _abi_leaf_at(
    parameter_definitions: list[ABIValue], indices: list[int]
) -> ABIValue | None:
    """Return the ABI tree node a proto data path points at, or None.

    The walk mirrors how paths are built in section 2. Worked example for
    `uniswapV3Swap(uint256 amount, uint256 minReturn, uint256[] pools)` with
    path `pools.[-1]`, i.e. indices `[2, -1]`:

        parameter_definitions[2]  == {"array": {"atomic": "ABI_UINT256"}}
        index -1 steps INTO the array; all elements share the one element
        type, so the node becomes   {"atomic": "ABI_UINT256"}   <- the leaf

    A path may also point at a whole array (trailing `.[]` iteration, no
    index appended): the firmware formats each element, so the element type
    is still the leaf — the final unwrap loop handles that.
    """
    if not indices or not 0 <= indices[0] < len(parameter_definitions):
        return None
    node = parameter_definitions[indices[0]]
    for idx in indices[1:]:
        if "array" in node:
            node = node["array"]  # element index: elements share one type
        elif "tuple" in node:
            fields = node["tuple"]["fields"]
            if not 0 <= idx < len(fields):
                return None
            node = fields[idx]
        else:
            return None  # path walks past a scalar leaf — shouldn't happen
    while "array" in node:
        node = node["array"]  # trailing `.[]`: the element is what's formatted
    return node


def _retype_uint_leaf_as_address(
    parameter_definitions: list[ABIValue], indices: list[int]
) -> bool:
    """Flip the uint ABI leaf at `indices` to ABI_ADDRESS, in place.

    See the section 4b comment for why this is sound (identical one-word
    encoding; dirty values degrade to blind signing on-device).

    Deliberate side effect: an array has ONE shared element type, so retyping
    `pools.[-1]` retypes every `pools` element — and any other field reading
    the same leaf sees an address from then on. That matches the author's
    intent (the whole array holds packed addresses) and is logged by the
    caller as an adjustment.

    Returns False if the walk fails or the leaf isn't a uint — the caller
    then treats the field as a plain formatter/type mismatch (DROP).
    """
    node = _abi_leaf_at(parameter_definitions, indices)
    if node is None:
        return False
    if node.get("atomic") == "ABI_ADDRESS":
        return True  # another field already retyped this shared leaf
    if str(node.get("atomic", "")).startswith("ABI_UINT"):
        node["atomic"] = "ABI_ADDRESS"
        return True
    return False


# ---------------------------------------------------------------------
# 4c. Calldata-bound fields, one helper per formatter's params
# ---------------------------------------------------------------------


def _build_path_field(
    field_def: dict[str, Any], ctx: _FormatContext
) -> ERC7730Field | None:
    """Build a displayed field bound to a calldata (or container) path."""
    if not _field_is_displayed(field_def, ctx):
        return None

    fmt = field_def["format"]  # present, else _field_is_displayed is False
    label = field_def.get("label", "")
    # A displayed field needs a label (the proto requires one); an empty label
    # would render blank on-device, so treat it as missing display info.
    if not label:
        raise UnsupportedFeature(
            "missing-label", f"{fmt} field at {field_def.get('path')!r}"
        )
    path_str = str(field_def.get("path") or "")
    if not path_str:
        raise UnsupportedFeature("missing-path", f"field {label!r}")

    # A `$.metadata.constants.*` field path is a constant, not calldata.
    if path_str.startswith("$"):
        const = _resolve_constant(path_str, ctx.constants)
        if const is None:
            raise UnsupportedFeature("descriptor-path", f"{path_str} (field {label!r})")
        return _const_value_field(label, fmt, const, ctx, already_resolved=True)

    try:
        path, kind = path_to_dict(path_str, ctx.inputs)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(e.feature, f"{e.detail} (field {label!r})") from None

    if fmt not in _FORMATTER_MAP:
        raise UnsupportedFeature("unsupported-formatter", f"{fmt} (field {label!r})")
    _check_kind_or_reinterpret(fmt, path, kind, path_str, label, ctx)

    out: ERC7730Field = {
        "path": path,
        "label": label,
        "formatter": _FORMATTER_MAP[fmt],
    }
    params = field_def.get("params") or {}
    if fmt == "tokenAmount":
        _apply_token_amount_params(out, params, path_str, label, ctx)
    elif fmt == "unit":
        _apply_unit_params(out, params, label)
    elif fmt == "date":
        _apply_date_params(out, params, path_str, label, ctx)
    elif fmt == "calldata":
        _apply_calldata_params(out, params, path_str, label, ctx)
    elif fmt == "enum":
        _apply_enum_params(out, params, path_str, label, ctx)
    return out


def _apply_token_amount_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Fill in FORMATTER_TOKEN_AMOUNT's token reference and threshold.

    A token amount must know WHICH token it denominates. A descriptor names
    the token one of three ways, tried in order:

      1. `tokenPath`  — the token's address is in calldata; emitted as a
         proto `token_path` the firmware walks.
      2. `token`      — a hardcoded address (or constants reference);
         emitted as `const_token_address`.
      3. neither      — the token is the null (zero) address. If the
         descriptor declares the zero address a native-currency sentinel,
         the value is a native amount: FORMATTER_TOKEN_AMOUNT is
         unconstructable without a token, so emit plain AMOUNT instead
         (adjustment). Otherwise it's an "unknown token" we can't render
         faithfully: DROP.
    """
    if params.get("tokenPath"):
        token_path_str = str(params["tokenPath"])
        try:
            tp_path, tp_kind = path_to_dict(token_path_str, ctx.inputs)
        except UnsupportedFeature as e:
            raise UnsupportedFeature(
                "unresolvable-token-path",
                f"{token_path_str}: [{e.feature}] {e.detail} (field {label!r})",
            ) from None
        if tp_kind == KIND_ADDRESS:
            out["token_path"] = tp_path
        elif (
            tp_kind == KIND_NUMERIC
            and "path" in tp_path
            and _retype_uint_leaf_as_address(ctx.parameter_definitions, tp_path["path"])
        ):
            # Packed-address pattern (section 4b), e.g. 1inch `order.takerAsset`.
            out["token_path"] = tp_path
            ctx.adjust(
                "token-address-in-numeric",
                f"{token_path_str} is numeric but used as token address — "
                f"ABI leaf retyped to address (field {label!r})",
            )
        else:
            raise UnsupportedFeature(
                "unresolvable-token-path",
                f"{token_path_str} is {tp_kind}, not an address (field {label!r})",
            )
    elif params.get("token"):
        const_addr = _resolve_address_ref(params["token"], ctx.constants)
        if const_addr is None:
            raise UnsupportedFeature(
                "invalid-const-token", f"{params['token']!r} (field {label!r})"
            )
        if int(const_addr, 16) == 0:
            # A literal zero address is the null/native token, not an ERC-20:
            # same treatment as the no-token case below.
            if not _native_currency_includes_zero(params, ctx.constants):
                raise UnsupportedFeature(
                    "tokenamount-unknown-token",
                    f"tokenAmount with null token (field {label!r})",
                )
            out["formatter"] = _FORMATTER_MAP["amount"]
            ctx.adjust(
                "tokenamount-native-as-amount",
                f"{path_str}: tokenAmount with zero-address token declared "
                f"native — emitted as AMOUNT (field {label!r})",
            )
        else:
            out["const_token_address"] = const_addr
    elif _native_currency_includes_zero(params, ctx.constants):
        out["formatter"] = _FORMATTER_MAP["amount"]
        ctx.adjust(
            "tokenamount-native-as-amount",
            f"{path_str}: tokenAmount with no token and a zero-address native "
            f"sentinel — emitted as AMOUNT (field {label!r})",
        )
    else:
        raise UnsupportedFeature(
            "tokenamount-unknown-token",
            f"tokenAmount with no token reference (field {label!r})",
        )

    # `threshold` ("unlimited above this") applies only to a real token amount
    # (calldata- or constant-addressed); the AMOUNT fallback ignores it.
    if "token_path" in out or "const_token_address" in out:
        _apply_threshold(out, params, label, ctx.constants)


def _apply_threshold(
    out: ERC7730Field, params: dict[str, Any], label: str, constants: dict[str, Any]
) -> None:
    """Normalize a `threshold` param to hex bytes; reject the unserializable."""
    threshold = params.get("threshold")
    if isinstance(threshold, str) and threshold.startswith("$"):
        resolved = _resolve_constant(threshold, constants)
        if resolved is None:
            raise UnsupportedFeature(
                "unresolvable-threshold", f"{threshold} (field {label!r})"
            )
        threshold = resolved
    if isinstance(threshold, str):
        normalized = _normalize_hex(threshold)
        # `_normalize_hex` doesn't validate: a non-hex value would slip through
        # and crash `bytes.fromhex` at serialization time. Reject it here.
        if set(normalized) - _HEX_DIGITS:
            raise UnsupportedFeature(
                "invalid-threshold", f"{threshold!r} (field {label!r})"
            )
        out["threshold"] = normalized
    elif isinstance(threshold, int):
        # A negative threshold has no byte encoding (`hex(-n)` yields a
        # `-0x…` string that also breaks `bytes.fromhex`).
        if threshold < 0:
            raise UnsupportedFeature(
                "invalid-threshold", f"{threshold} (field {label!r})"
            )
        out["threshold"] = _normalize_hex(hex(threshold))


def _apply_unit_params(
    out: ERC7730Field, params: dict[str, Any], label: str
) -> None:
    """Copy FORMATTER_UNIT's decimals / base / prefix params."""
    if params.get("decimals") is not None:
        try:
            decimals = int(params["decimals"])
        except (TypeError, ValueError):
            raise UnsupportedFeature(
                "invalid-decimals", f"{params['decimals']!r} (field {label!r})"
            ) from None
        # `decimals` is a proto uint32 — out-of-range values can't serialize.
        if not 0 <= decimals <= 0xFFFFFFFF:
            raise UnsupportedFeature(
                "invalid-decimals",
                f"{decimals} out of uint32 range (field {label!r})",
            )
        out["decimals"] = decimals
    if params.get("base"):
        out["base"] = str(params["base"])
    if params.get("prefix") is not None:
        out["prefix"] = bool(params["prefix"])


def _apply_date_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Downgrade non-timestamp `date` encodings to RAW.

    FORMATTER_DATE renders a unix timestamp (seconds). The `blockheight`
    encoding is a plain block number, not a time — the date formatter would
    misrender it — so anything but `timestamp` falls back to the raw integer.
    """
    if params.get("encoding", "timestamp") != "timestamp":
        out["formatter"] = _FORMATTER_MAP["raw"]
        ctx.adjust(
            "date-encoding-as-raw",
            f"{path_str}: date with encoding {params.get('encoding')!r} — "
            f"emitted as RAW integer (field {label!r})",
        )


def _apply_calldata_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Fill in FORMATTER_CALLDATA's callee path and selector.

    The embedded calldata is rendered with the *called contract's* display
    format, so the firmware must know the callee address: `calleePath` is
    required (the firmware raises without it). When the embedded blob is
    args-only, `selector` names the nested function's 4-byte selector.

    Params beyond those two (`amountPath`, `spenderPath`, …) have no proto
    representation; the nested call still renders in full via the callee's
    display format, so they are dropped and logged as an adjustment rather
    than dropping the field.
    """
    callee_str = params.get("calleePath")
    if not callee_str:
        raise UnsupportedFeature(
            "calldata-missing-callee", f"{path_str} (field {label!r})"
        )
    try:
        cp_path, cp_kind = path_to_dict(str(callee_str), ctx.inputs)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(
            "unresolvable-callee-path",
            f"{callee_str}: [{e.feature}] {e.detail} (field {label!r})",
        ) from None
    if cp_kind == KIND_ADDRESS:
        out["callee_path"] = cp_path
    elif (
        cp_kind == KIND_NUMERIC
        and "path" in cp_path
        and _retype_uint_leaf_as_address(ctx.parameter_definitions, cp_path["path"])
    ):
        # Packed-address pattern (section 4b), same as tokenPath.
        out["callee_path"] = cp_path
        ctx.adjust(
            "callee-address-in-numeric",
            f"{callee_str} is numeric but used as callee address — "
            f"ABI leaf retyped to address (field {label!r})",
        )
    else:
        raise UnsupportedFeature(
            "unresolvable-callee-path",
            f"{callee_str} is {cp_kind}, not an address (field {label!r})",
        )

    selector = params.get("selector")
    if selector is not None:
        normalized = _normalize_hex(str(selector))
        if len(normalized) != 8 or not _is_hex(normalized):
            raise UnsupportedFeature(
                "invalid-calldata-selector", f"{selector!r} (field {label!r})"
            )
        out["selector"] = normalized

    ignored = sorted(set(params) - {"calleePath", "selector"})
    if ignored:
        ctx.adjust(
            "calldata-params-ignored",
            f"{path_str}: {', '.join(ignored)} have no proto representation "
            f"(field {label!r})",
        )


def _apply_enum_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Resolve an enum field's `$.metadata.enums.*` reference into enum_values.

    The descriptor maps calldata keys to display strings, e.g.
    `{"0": "Stable", "1": "Variable"}`. Keys must fit uint32; the JSON-boolean
    variant (`True`/`False` keys over a bool calldata value) maps to 1/0 and
    is logged as an adjustment. Every accepted enum field is logged too, so
    the feature's use is visible while firmware support is fresh.
    """
    ref = params.get("$ref")
    if not ref:
        raise UnsupportedFeature("enum-missing-ref", f"{path_str} (field {label!r})")
    parts = _descriptor_path_parts(str(ref))
    entries = None
    if parts is not None and parts[:2] == ("metadata", "enums"):
        entries = ctx.enums.get(parts[2])
    if not isinstance(entries, dict) or not entries:
        raise UnsupportedFeature(
            "unresolvable-enum-ref", f"{ref!r} (field {label!r})"
        )

    values: list[dict[str, Any]] = []
    bool_keys = False
    for k, v in entries.items():
        ks = str(k)
        if ks in ("True", "true", "False", "false"):
            key = 1 if ks.lower() == "true" else 0
            bool_keys = True
        else:
            try:
                key = int(ks, 0)
            except ValueError:
                raise UnsupportedFeature(
                    "invalid-enum-entry", f"key {k!r} in {ref!r} (field {label!r})"
                ) from None
        # keys ride in a proto uint32
        if not 0 <= key <= 0xFFFFFFFF:
            raise UnsupportedFeature(
                "invalid-enum-entry",
                f"key {k!r} out of uint32 range in {ref!r} (field {label!r})",
            )
        values.append({"key": key, "value": str(v)})

    if bool_keys:
        ctx.adjust(
            "enum-bool-keys",
            f"{ref!r}: True/False keys mapped to 1/0 (field {label!r})",
        )
    ctx.adjust(
        "enum-field",
        f"{path_str}: {len(values)} value(s) from {ref!r} (field {label!r})",
    )
    out["enum_values"] = values


# =====================================================================
# 5. Descriptor -> display-format records
# =====================================================================


def load_descriptor(path: Path) -> dict[str, Any]:
    """Load an ERC-7730 descriptor, recursively inlining any `includes`.

    Uses the `erc7730` library's merge: the calling file's keys win on
    conflict; `includes` may be a string or a list of sibling paths.
    """
    return read_json_with_includes(path)


def load_display_formats(
    path: Path,
    unsupported: list[tuple[str, str, str]] | None = None,
    adjustments: list[tuple[str, str, str]] | None = None,
) -> list[ERC20DisplayFormat]:
    """Convenience: `load_descriptor` + `build_display_formats`."""
    descriptor = load_descriptor(path)
    source = f"{path.parent.name}/{path.name}"
    return build_display_formats(
        descriptor, source=source, unsupported=unsupported, adjustments=adjustments
    )


def _get_intent(display_format: dict[str, Any]) -> str:
    intent = display_format.get("intent", "")
    if isinstance(intent, dict):
        return intent.get("en") or next(iter(intent.values()), "")
    return intent or ""


# One display format ready for deployment expansion:
# (func_sig_hex, intent, parameter_definitions, field_definitions)
_Candidate = tuple[str, str, list[ABIValue], list[ERC7730Field]]


def _build_one_format(
    sig_key: str,
    display_format: dict[str, Any],
    definitions: dict[str, Any],
    constants: dict[str, Any],
    enums: dict[str, Any],
    source: str,
) -> tuple[_Candidate | None, list[tuple[str, str]], list[tuple[str, str]]]:
    """Build ONE display format (one function signature).

    Returns `(candidate, issues, adjustments)`:
      * `candidate` — the buildable display format, or None;
      * `issues` — every unsupported feature hit, as (feature, detail).
        NON-EMPTY MEANS THE FORMAT IS DROPPED — but the field loop keeps
        scanning after the first issue so the log shows everything wrong with
        a signature at once, not just the first find.
      * `adjustments` — (kind, detail) for fields accepted with modification;
        only meaningful when `issues` is empty.
    """
    issues: list[tuple[str, str]] = []

    if sig_key.startswith("0x"):
        # Hex selector — we can't derive parameter types without an ABI.
        issues.append(("selector-only-entry", sig_key))
        return None, issues, []
    try:
        parsed: Function = parse_signature(sig_key)
    except Exception as e:
        issues.append(("unparseable-signature", f"{sig_key}: {e}"))
        return None, issues, []

    func_sig_hex = signature_to_selector(compute_signature(parsed))
    # The selector is our own 4-byte computation; guard the invariant.
    if len(func_sig_hex) != 10 or not _is_hex(func_sig_hex[2:]):
        LOG.warning("%s: skipping %s — bad selector %r", source, sig_key, func_sig_hex)
        return None, issues, []
    inputs = list(parsed.inputs or [])

    try:
        parameter_definitions = [build_abi_value(p) for p in inputs]
    except UnsupportedFeature as e:
        issues.append((e.feature, f"{sig_key}: {e.detail}"))
        return None, issues, []
    except ValueError as e:
        issues.append(("unrepresentable-params", f"{sig_key}: {e}"))
        return None, issues, []

    ctx = _FormatContext(inputs, constants, parameter_definitions, enums=enums)
    field_defs: list[ERC7730Field] = []
    for f in display_format.get("fields", []):
        if not isinstance(f, dict):
            issues.append(("malformed-field-entry", f"{sig_key}: {f!r}"))
            continue
        resolved = _resolve_ref(f, definitions)
        if resolved is None:
            issues.append(("unresolvable-ref", f"{sig_key}: {f.get('$ref')!r}"))
            continue
        f = resolved
        if isinstance(f.get("fields"), list):
            # A nested field group (a `path` scoping sub-`fields`, e.g.
            # `#.marketParams` in Morpho Blue). The group itself has no
            # `format`, so it would otherwise be skipped as hidden — but its
            # sub-fields ARE displayed and we can't express their relative
            # paths, so this drops the display format instead.
            issues.append(("nested-fields", f"{sig_key}: path {f.get('path')!r}"))
            continue
        try:
            built = (
                _build_path_field(f, ctx)
                if "path" in f
                else _build_non_path_field(f, ctx)
            )
        except UnsupportedFeature as e:
            issues.append((e.feature, f"{sig_key}: {e.detail}"))
            continue
        if built is not None:
            field_defs.append(built)

    if issues:
        return None, issues, []
    intent = _get_intent(display_format)
    return (func_sig_hex, intent, parameter_definitions, field_defs), [], ctx.adjustments


def build_display_formats(
    descriptor: dict[str, Any],
    source: str = "<descriptor>",
    unsupported: list[tuple[str, str, str]] | None = None,
    adjustments: list[tuple[str, str, str]] | None = None,
) -> list[ERC20DisplayFormat]:
    """Turn a single (post-includes) ERC-7730 descriptor into a list of records.

    Yields one record per (deployment x signature) pair.

    Dropping is per display format (signature): a format with any unsupported
    feature is dropped whole — we never emit one with a field silently
    missing — while the other, clean formats in the same file are still
    emitted. Every distinct feature found is appended to `unsupported` as
    `(source, feature, detail)`; every accepted-but-modified field of an
    *emitted* format is appended to `adjustments` as `(source, kind, detail)`.

    If NO display format survives, `UnsupportedFeature` is raised so the
    caller can treat the whole file as skipped.
    """
    context = descriptor.get("context") or {}
    contract = context.get("contract") or {}
    deployments = contract.get("deployments") or []
    display = descriptor.get("display") or {}
    formats = display.get("formats") or {}
    definitions = display.get("definitions") or {}
    metadata = descriptor.get("metadata") or {}
    constants = metadata.get("constants") or {}
    enums = metadata.get("enums") or {}
    if not deployments:
        LOG.info("%s: no deployments, skipping", source)
        return []

    # Distinct (feature, detail) pairs found in THIS file, deduped for the
    # log. The drop decision is per format (issues from _build_one_format),
    # NOT this dedup — a feature repeated across two signatures must drop both.
    file_features: list[tuple[str, str]] = []
    seen_features: set[tuple[str, str]] = set()

    candidates: list[_Candidate] = []
    for sig_key, display_format in formats.items():
        candidate, issues, format_adjustments = _build_one_format(
            sig_key, display_format, definitions, constants, enums, source
        )
        for feature, detail in issues:
            LOG.info("%s: unsupported %s: %s", source, feature, detail)
            if (feature, detail) not in seen_features:
                seen_features.add((feature, detail))
                file_features.append((feature, detail))
        if candidate is None:
            continue
        if adjustments is not None:
            adjustments.extend(
                (source, kind, f"{sig_key}: {detail}")
                for kind, detail in format_adjustments
            )
        candidates.append(candidate)

    # Report every feature found, even when other formats were still emitted.
    if unsupported is not None:
        unsupported.extend((source, feat, detail) for feat, detail in file_features)

    # Nothing representable in the whole file — let the caller skip it.
    if not candidates and file_features:
        raise UnsupportedFeature(
            "descriptor-skipped",
            f"{source}: {len(file_features)} unsupported feature(s)",
        )

    return _expand_deployments(candidates, deployments, source)


def _expand_deployments(
    candidates: list[_Candidate],
    deployments: list[dict[str, Any]],
    source: str,
) -> list[ERC20DisplayFormat]:
    """Cross the per-signature candidates with the contract's deployments.

    Deployment sanity problems (bad address, bad chain id) skip just that
    deployment with a warning — they are registry data errors on the record
    key, not unsupported display features.
    """
    results: list[ERC20DisplayFormat] = []
    for func_sig_hex, intent, parameter_definitions, field_defs in candidates:
        for deployment in deployments:
            chain_id = deployment.get("chainId")
            address = deployment.get("address")
            if chain_id is None or not address:
                LOG.warning("%s: incomplete deployment %r", source, deployment)
                continue
            try:
                chain_id_int = int(chain_id)
            except (TypeError, ValueError):
                LOG.warning("%s: non-integer chainId %r", source, chain_id)
                continue
            if chain_id_int <= 0:
                LOG.warning("%s: non-positive chainId %r", source, chain_id)
                continue
            address_hex = "0x" + _normalize_hex(address)
            if len(address_hex) != 42 or not _is_hex(address_hex[2:]):
                LOG.warning("%s: invalid address %r", source, address)
                continue
            results.append(
                {
                    "chain_id": chain_id_int,
                    "address": address_hex,
                    "func_sig": func_sig_hex,
                    "intent": intent,
                    "parameter_definitions": parameter_definitions,
                    "field_definitions": field_defs,
                }
            )

    return results
