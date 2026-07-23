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


class UnsupportedFeature(Exception):
    """A *displayed* field uses a feature we can't faithfully represent.

    Raised so the caller skips the entire descriptor file rather than emitting
    a display format that silently omits a field. Carries a stable `feature`
    tag (for aggregation in the unsupported-features log) and a human-readable
    `detail`.
    """

    def __init__(self, feature: str, detail: str):
        self.feature = feature
        self.detail = detail
        super().__init__(f"{feature}: {detail}")


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

    Fixed-size arrays (`[N]`) cannot be represented in our ABI value model (the
    proto `ABIValue` carries no element count), so any fixed dimension raises
    `UnsupportedFeature` and the whole descriptor is skipped.
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
    """Turn a `Component` into the nested-dict mirror of EthereumABIValueInfo."""
    base_type, array_depth = _split_array_suffix(c.type)

    base: ABIValue
    if base_type == "tuple":
        # The firmware's array decoder (`ABIValue.from_proto` in
        # clear_signing.py) only models a tuple nested in a *single* array
        # layer; `tuple[][]` (and deeper) raise `InvalidFormatDefinition`
        # on-device, so refuse to emit them.
        if array_depth >= 2:
            raise UnsupportedFeature(
                "tuple-in-nested-array",
                f"{c.type} (a tuple may be nested in at most one array)",
            )
        sub_components = c.components or []
        # The firmware decodes every tuple field as an atomic/dynamic *leaf*
        # (`_get_leaf_parser` in clear_signing.py raises `InvalidFormatDefinition`
        # for anything else), so a tuple field that is itself a tuple or an array
        # (`uint256[]`, `tuple[]`, …) can't be represented. A leaf never starts
        # with `tuple` nor ends with `]`.
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
            # The firmware decodes an array's tuple elements via an offset table
            # (`Array._parse_body` in clear_signing.py), which is how *dynamic*
            # tuples are ABI-encoded. A static tuple inside an array is encoded
            # inline with a fixed stride and no offsets, so the firmware would
            # misread it — refuse to emit rather than ship a wrong decode.
            raise UnsupportedFeature(
                "static-tuple-in-array",
                f"{c.type} (static tuples inside arrays are not supported)",
            )
        # When the tuple sits inside an array, the array layer carries dynamism
        # and the firmware always parses the in-array tuple as static; only a
        # top-level tuple carries its own dynamism.
        is_dynamic = tuple_is_dynamic and not array_depth
        tup: ABITuple = {
            "fields": [build_abi_value(sub) for sub in sub_components],
            "is_dynamic": is_dynamic,
        }
        base = {"tuple": tup}
    else:
        if base_type not in _ABI_TYPE_MAP:
            raise ValueError(f"unknown ABI type: {c.type}")
        # The firmware models an atomic/dynamic leaf nested in at most *two*
        # array layers; `T[][][]` (and deeper) raise `InvalidFormatDefinition`
        # on-device.
        if array_depth >= 3:
            raise UnsupportedFeature(
                "array-nesting-too-deep",
                f"{c.type} (arrays may be nested at most two deep)",
            )
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

# Leaf-value kinds used for formatter ↔ type compatibility checks.
KIND_ADDRESS = "address"
KIND_NUMERIC = "numeric"  # any uint*
KIND_BYTES = "bytes"  # bool / bytesN / bytes / string — a scalar leaf only `raw` renders
KIND_OTHER = "other"  # un-indexed array, tuple, or unknown — nothing renders it as one field


def _classify_kind(base_type: str, array_depth: int) -> str:
    """Classify a resolved leaf Solidity type into a formatter-compat kind."""
    if array_depth > 0:
        # The leaf is still an array (e.g. an un-indexed `uint256[]`); no scalar
        # formatter can render it (a `.[]` iteration peels the dimension first).
        return KIND_OTHER
    if base_type == "address":
        return KIND_ADDRESS
    if base_type.startswith("uint"):
        return KIND_NUMERIC
    if base_type == "bool" or base_type == "string" or base_type.startswith("bytes"):
        # A scalar bool / bytesN / bytes / string leaf — the firmware's
        # RawFormatter renders these (bytes as hex, bool as text, string as-is),
        # but no other formatter does.
        return KIND_BYTES
    # tuple / unknown — not representable as a single rendered value.
    return KIND_OTHER


def path_to_dict(path_str: str, inputs: list[Component]) -> tuple[ERC7730Path, str]:
    """Convert an ERC-7730 path string to `(proto path, leaf kind)`.

    The leaf kind is one of `KIND_ADDRESS` / `KIND_NUMERIC` / `KIND_BYTES` /
    `KIND_OTHER`, used by the caller to check the field's formatter against
    the type the path actually points at.

    A trailing `.[]` (whole-array iteration) is supported: the path points at
    the array itself and the firmware formats each element. The peeled element
    kind is returned, so formatter compatibility is checked against the element
    type (e.g. `amounts.[]` over `uint256[]` is numeric).

    Every unsupported path raises `UnsupportedFeature` with a distinct feature
    tag, so the drop reason is visible in the log:
      * `descriptor-path` — `$.…` paths (callers resolve constants *before*
        calling; one reaching this function is not a constants lookup we handle)
      * `per-element-field-path` — anything following a `.[]`
        (e.g. `swaps.[].amount`), which can't be expressed as a flat index path
      * `array-slice-path` — `x.[a:b]` slices, not representable in the proto
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

    indices: list[int] = []
    current = inputs
    leaf_base: str | None = None
    leaf_array_depth = 0
    saw_array_iter = False
    for element in parsed.elements:
        if saw_array_iter:
            # A `.[]` resolves the path to the whole array, which the firmware
            # formats element-by-element — so nothing may follow it. A per-element
            # field extraction (`swaps.[].amount`) can't be expressed as a flat
            # index path, so reject it.
            raise UnsupportedFeature("per-element-field-path", path_str)
        if isinstance(element, PathField):
            name_to_idx = {p.name: i for i, p in enumerate(current) if p.name}
            if element.identifier not in name_to_idx:
                raise UnsupportedFeature(
                    "unknown-path-segment", f"{path_str} (segment {element.identifier!r})"
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
            # `.[]` whole-array iteration. The proto path points at the array
            # itself (no index appended); the firmware applies the field's
            # formatter to each element and joins the results. Peel one array
            # dimension so the leaf kind reflects the per-element type — a leaf
            # that is still an array (e.g. `uint256[][]`) then classifies as
            # KIND_OTHER, since only flat arrays of scalars can be iterated.
            if leaf_array_depth <= 0:
                raise UnsupportedFeature("iteration-over-non-array", path_str)
            leaf_array_depth -= 1
            saw_array_iter = True
        elif isinstance(element, ArraySlice):
            raise UnsupportedFeature("array-slice-path", path_str)
        else:
            raise UnsupportedFeature(
                "unparseable-path", f"{path_str}: unhandled element {element!r}"
            )

    kind = KIND_OTHER if leaf_base is None else _classify_kind(leaf_base, leaf_array_depth)
    return {"path": indices}, kind


# =====================================================================
#                              Field building
# =====================================================================


_FORMATTER_MAP = {
    "addressName": "FORMATTER_ADDRESS_NAME",
    "amount": "FORMATTER_AMOUNT",
    "tokenAmount": "FORMATTER_TOKEN_AMOUNT",
    "unit": "FORMATTER_UNIT",
    # The firmware renders `raw` per Solidity type (int as decimal, address/bytes
    # as hex, bool as text, string as-is) and `date` as a human-readable unix
    # timestamp. A `date` with a `blockheight` encoding is overridden to RAW in
    # build_field_dict, since it's a block number rather than a time.
    "raw": "FORMATTER_RAW",
    "date": "FORMATTER_DATE",
    # `calldata` (embedded calldata of a nested call) has no dedicated on-device
    # formatter yet; the bytes are shown as hex via RAW (adjustment logged in
    # build_field_dict). A faithful rendering (calleePath / selector decoding)
    # needs a new proto formatter and firmware support first.
    "calldata": "FORMATTER_RAW",
}

# The leaf-value kind(s) each formatter accepts. A field whose path resolves to a
# kind outside this set is unrepresentable, so the descriptor is skipped —
# except `addressName`, which build_field_dict reinterprets on numeric/bytes
# leaves (logged as an adjustment) instead of skipping.
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
}

# (kind, detail) pairs describing accepted-but-modified fields; collected per
# field into build_display_formats' `adjustments` output so every subtle
# manipulation (formatter overrides, ABI retypes, constant materialization) is
# visible in the processing log.
Adjustments = list[tuple[str, str]]


def _adjust(adjustments: Adjustments | None, kind: str, detail: str) -> None:
    LOG.info("adjustment %s: %s", kind, detail)
    if adjustments is not None:
        adjustments.append((kind, detail))


def _retype_numeric_leaf_to_address(
    parameter_definitions: list[ABIValue] | None, indices: list[int]
) -> bool:
    """Retype the numeric ABI leaf at `indices` to ABI_ADDRESS, in place.

    Used when a descriptor formats a uint-typed value as an address
    (`addressName` / `tokenPath` on a numeric leaf): the value *is* an address
    stuffed into a uint, and `address` and `uintN` share the same static
    one-word ABI layout, so retyping only changes how the firmware decodes and
    renders the word. A value with non-zero high bytes raises `DirtyAddress`
    on-device, which safely falls back to blind signing.

    An array's element type is shared, so retyping through an index or a
    trailing `.[]` iteration deliberately affects every element (and any other
    field reading the same leaf) — callers log the adjustment.

    Returns False (caller should skip instead) if the walk fails or the leaf
    isn't a uint; True if retyped (or already an address).
    """
    if parameter_definitions is None or not indices:
        return False
    if not 0 <= indices[0] < len(parameter_definitions):
        return False
    node = parameter_definitions[indices[0]]
    for idx in indices[1:]:
        if "array" in node:
            node = node["array"]  # an element index; the element type is shared
        elif "tuple" in node:
            fields = node["tuple"]["fields"]
            if not 0 <= idx < len(fields):
                return False
            node = fields[idx]
        else:
            return False
    # A trailing `.[]` iteration leaves the path pointing at the array itself;
    # the per-element leaf is what gets formatted, so retype that.
    while "array" in node:
        node = node["array"]
    if node.get("atomic") == "ABI_ADDRESS":
        return True  # already retyped by another field on the same leaf
    if str(node.get("atomic", "")).startswith("ABI_UINT"):
        node["atomic"] = "ABI_ADDRESS"
        return True
    return False


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


def _parse_descriptor_path_3(path_str: str) -> tuple[str, str, str] | None:
    """Parse a `$.a.b.c` descriptor path and return `(a, b, c)`.

    Returns None if the path can't be parsed or doesn't have exactly three
    field-name elements.
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
    return e0.identifier, e1.identifier, e2.identifier


def _resolve_constant(path_str: str, constants: dict[str, Any]) -> Any | None:
    """Resolve a `$.metadata.constants.<key>` path against `metadata.constants`.

    Returns the constant value, or None if the path isn't a constants lookup
    or the key is missing.
    """
    parts = _parse_descriptor_path_3(path_str)
    if parts is None or parts[:2] != ("metadata", "constants"):
        return None
    return constants.get(parts[2])


def _resolve_address_ref(value: Any, constants: dict[str, Any]) -> str | None:
    """Resolve a token-address reference to normalized 20-byte hex (no `0x`).

    ``value`` is a literal address string or a ``$.metadata.constants.*``
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

    A `tokenAmount` with no `tokenPath`/`token` has a null (zero-address) token.
    When the descriptor declares the zero address as a native-currency sentinel,
    that null token *is* the chain's native currency, so the amount is native.
    Entries may be literal addresses or `$.metadata.constants.*` references.
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

    Field-level keys override the definition; ``params`` dicts are deep-merged
    so the field can add keys (e.g. ``tokenPath``) without losing definition
    keys (e.g. ``nativeCurrencyAddress``).

    Returns None for an unresolvable `$ref` (not a `$.display.definitions.*`
    path, or a missing definition). The field's display info would come from
    that definition, so leaving the ref unmerged risks silently dropping a
    displayed field; the caller must skip instead.
    """
    ref = field_def.get("$ref")
    if ref is None:
        return field_def

    parts = _parse_descriptor_path_3(str(ref))
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


def _build_const_value_field(
    label: str,
    fmt: str,
    raw_value: Any,
    constants: dict[str, Any],
    adjustments: Adjustments | None,
) -> ERC7730Field:
    """Build a field bound to a constant value instead of a calldata path.

    `raw_value` is a literal or a `$.metadata.constants.*` reference. The value
    rides in the proto as a `const_value` path, which the firmware renders
    as-is via the raw formatter. Raises `UnsupportedFeature` if the constant
    can't be resolved or the format isn't `raw` (no other on-device formatter
    accepts a pre-rendered string).
    """
    value = raw_value
    if isinstance(value, str) and value.startswith("$"):
        resolved = _resolve_constant(value, constants)
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
        _adjust(
            adjustments,
            "constant-value-stringified",
            f"{value!r} rendered as {rendered!r} (field {label!r})",
        )
    _adjust(
        adjustments,
        "constant-value-field",
        f"field {label!r} bound to constant {rendered!r}",
    )
    return {
        "path": {"const_value": rendered},
        "label": label,
        "formatter": _FORMATTER_MAP["raw"],
    }


def _field_is_displayed(field_def: dict[str, Any]) -> bool:
    """Whether a field is meant to be shown to the user.

    Not-displayed fields carry no display information, so we skip them without
    validating their path/formatter/type — they are never "missing fields".
    A field is hidden when it has no `format`, or `visible` is explicitly
    `never` / `false`.
    """
    if field_def.get("format") is None:
        return False
    return field_def.get("visible") not in (False, "never")


def build_field_dict(
    field_def: dict[str, Any],
    inputs: list[Component],
    constants: dict[str, Any] | None = None,
    label_context: str = "",
    parameter_definitions: list[ABIValue] | None = None,
    adjustments: Adjustments | None = None,
) -> ERC7730Field | None:
    """Convert a single ERC-7730 field definition.

    Returns None for a not-displayed (hidden) field — skipped without checks.
    Raises `UnsupportedFeature` for a *displayed* field we cannot faithfully
    represent, so the caller skips the whole descriptor file rather than emit a
    display format with a missing field.

    `parameter_definitions` (this display format's ABI value dicts) is mutated
    in place when a field forces a leaf reinterpretation — currently
    `addressName`/`tokenPath` on a uint leaf retypes it to ABI_ADDRESS. Every
    such accepted-but-modified field is recorded into `adjustments`.
    """
    label = field_def.get("label", "")

    # Hidden fields carry no display info — skip before any validation so an
    # unrepresentable path/formatter on something we never show is a non-issue.
    if not _field_is_displayed(field_def):
        return None

    fmt = field_def["format"]  # guaranteed present by _field_is_displayed

    # A displayed field needs a label (proto requires it); an empty one renders
    # blank on-device, so treat it as missing display info.
    if not label:
        raise UnsupportedFeature(
            "missing-label", f"{fmt} field at {field_def.get('path')!r}"
        )

    path_str = field_def.get("path")
    if not path_str:
        raise UnsupportedFeature("missing-path", f"{label_context}: field {label!r}")

    constants = constants or {}

    # A `$.metadata.constants.*` field path is a constant, not calldata: resolve
    # it here and emit it as a const_value field (the value already resolved, so
    # pass empty constants down).
    if str(path_str).startswith("$"):
        const = _resolve_constant(str(path_str), constants)
        if const is None:
            raise UnsupportedFeature(
                "descriptor-path", f"{path_str} (field {label!r})"
            )
        return _build_const_value_field(label, fmt, const, {}, adjustments)

    try:
        path, value_kind = path_to_dict(str(path_str), inputs)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(e.feature, f"{e.detail} (field {label!r})") from None

    if fmt not in _FORMATTER_MAP:
        raise UnsupportedFeature("unsupported-formatter", f"{fmt} (field {label!r})")

    allowed_kinds = _FORMATTER_VALUE_KIND.get(fmt)
    if allowed_kinds is not None and value_kind not in allowed_kinds:
        # `addressName` on a non-address scalar is reinterpreted, not skipped:
        # descriptors routinely pack addresses into uints (1inch pools, maker
        # order receivers) or bytes words.
        reinterpreted = False
        if fmt == "addressName":
            if (
                value_kind == KIND_NUMERIC
                and "path" in path
                and _retype_numeric_leaf_to_address(
                    parameter_definitions, path["path"]
                )
            ):
                # The uint leaf now decodes as an address on-device; a value
                # with non-zero high bytes falls back to blind signing.
                _adjust(
                    adjustments,
                    "address-in-numeric",
                    f"{path_str} is numeric but formatted as addressName — "
                    f"ABI leaf retyped to address (field {label!r})",
                )
                reinterpreted = True
            elif value_kind == KIND_BYTES:
                # The firmware's AddressNameFormatter accepts bytes/str values
                # directly and renders them as a hex "address" — keep the
                # declared ABI type and let it do so.
                _adjust(
                    adjustments,
                    "addressname-on-bytes",
                    f"{path_str} is {value_kind} but formatted as addressName — "
                    f"rendered as hex (field {label!r})",
                )
                reinterpreted = True
        if not reinterpreted:
            raise UnsupportedFeature(
                "formatter-type-mismatch",
                f"{fmt} expects {'/'.join(sorted(allowed_kinds))} but {path_str!r} is "
                f"{value_kind} (field {label!r})",
            )

    out: ERC7730Field = {
        "path": path,
        "label": label,
        "formatter": _FORMATTER_MAP[fmt],
    }

    params = field_def.get("params") or {}
    if fmt == "calldata":
        # Embedded calldata shown as raw hex bytes for now (see _FORMATTER_MAP);
        # calleePath/selector params carry no meaning under RAW and are dropped.
        dropped = ", ".join(sorted(params)) if params else "none"
        _adjust(
            adjustments,
            "calldata-as-raw",
            f"{path_str} shown as raw bytes (params dropped: {dropped}) "
            f"(field {label!r})",
        )
    elif fmt == "tokenAmount":
        token_path_str = params.get("tokenPath")
        if token_path_str:
            try:
                tp_path, tp_kind = path_to_dict(str(token_path_str), inputs)
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
                and _retype_numeric_leaf_to_address(
                    parameter_definitions, tp_path["path"]
                )
            ):
                # Same packed-address pattern as addressName: the token address
                # lives in a uint leaf (e.g. 1inch `order.takerAsset`).
                out["token_path"] = tp_path
                _adjust(
                    adjustments,
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
            # A hardcoded / constant token address isn't in calldata; the proto
            # carries it directly as `const_token_address` (often a
            # `$.metadata.constants.*` reference resolved here). The firmware uses
            # it in place of a calldata-derived `token_path`.
            const_addr = _resolve_address_ref(params["token"], constants)
            if const_addr is None:
                raise UnsupportedFeature(
                    "invalid-const-token", f"{params['token']!r} (field {label!r})"
                )
            if int(const_addr, 16) == 0:
                # The zero address is the null/native token, not a real ERC-20.
                # Treat it like the no-token case: native if declared, else skip.
                if _native_currency_includes_zero(params, constants):
                    out["formatter"] = _FORMATTER_MAP["amount"]
                    _adjust(
                        adjustments,
                        "tokenamount-native-as-amount",
                        f"{path_str}: tokenAmount with zero-address token declared "
                        f"native — emitted as AMOUNT (field {label!r})",
                    )
                else:
                    raise UnsupportedFeature(
                        "tokenamount-unknown-token",
                        f"tokenAmount with null token (field {label!r})",
                    )
            else:
                out["const_token_address"] = const_addr
        elif _native_currency_includes_zero(params, constants):
            # No `tokenPath`/`token`: the token defaults to the null address, and
            # the descriptor lists the zero address in `nativeCurrencyAddress`,
            # declaring this amount as native currency. `FORMATTER_TOKEN_AMOUNT`
            # is meaningless (and unconstructable on-device) without a token, so
            # emit the native `AMOUNT` formatter instead.
            out["formatter"] = _FORMATTER_MAP["amount"]
            _adjust(
                adjustments,
                "tokenamount-native-as-amount",
                f"{path_str}: tokenAmount with no token and a zero-address native "
                f"sentinel — emitted as AMOUNT (field {label!r})",
            )
        else:
            # No token and no native sentinel. Per the ERC-7730 spec this is an
            # "unknown token" shown as a raw value with a warning — for which we
            # have no faithful formatter — so skip rather than mislabel.
            raise UnsupportedFeature(
                "tokenamount-unknown-token",
                f"tokenAmount with no token reference (field {label!r})",
            )
        # `threshold` applies only to a real token amount (calldata- or
        # constant-addressed); the native `AMOUNT` fallback ignores it on-device.
        if "token_path" in out or "const_token_address" in out:
            threshold = params.get("threshold")
            if isinstance(threshold, str) and threshold.startswith("$"):
                resolved_const = _resolve_constant(threshold, constants)
                if resolved_const is None:
                    raise UnsupportedFeature(
                        "unresolvable-threshold", f"{threshold} (field {label!r})"
                    )
                threshold = resolved_const
            if isinstance(threshold, str):
                normalized = _normalize_hex(threshold)
                # `_normalize_hex` doesn't validate: a non-hex value would slip
                # through and crash `bytes.fromhex` at serialization. Reject it
                # here as an unrepresentable field instead.
                # TODO: Revisit for a better validation logic. maybe do validate while normalizing.
                if set(normalized) - _HEX_DIGITS:
                    raise UnsupportedFeature(
                        "invalid-threshold", f"{threshold!r} (field {label!r})"
                    )
                out["threshold"] = normalized
            elif isinstance(threshold, int):
                # A negative threshold has no valid byte encoding (`hex(-n)`
                # yields a `-0x…` string that also breaks `bytes.fromhex`).
                if threshold < 0:
                    raise UnsupportedFeature(
                        "invalid-threshold", f"{threshold} (field {label!r})"
                    )
                out["threshold"] = _normalize_hex(hex(threshold))
    elif fmt == "unit":
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
    elif fmt == "date":
        # FORMATTER_DATE renders a unix timestamp (seconds) as a human-readable
        # date on-device. The `blockheight` encoding is a plain block number, not
        # a time — the date formatter would misrender it — so fall back to the
        # raw integer for anything other than a timestamp.
        if params.get("encoding", "timestamp") != "timestamp":
            out["formatter"] = _FORMATTER_MAP["raw"]
            _adjust(
                adjustments,
                "date-encoding-as-raw",
                f"{path_str}: date with encoding {params.get('encoding')!r} — "
                f"emitted as RAW integer (field {label!r})",
            )

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


def build_display_formats(
    descriptor: dict[str, Any],
    source: str = "<descriptor>",
    unsupported: list[tuple[str, str, str]] | None = None,
    adjustments: list[tuple[str, str, str]] | None = None,
) -> list[ERC20DisplayFormat]:
    """Turn a single (post-includes) ERC-7730 descriptor into a list of records.

    Yields one record per (deployment x signature) pair.

    Skipping is per *display format* (signature): a display format with any
    unsupported feature (a displayed field we can't represent, an unparseable
    signature, a selector-only entry, …) is dropped whole — we never emit a
    display format with a field silently missing — but the other clean display
    formats in the same file are still emitted. Every distinct feature found is
    appended to `unsupported` (if given) as `(source, feature, detail)` for
    later logging.

    Accepted-but-modified fields (formatter overrides, ABI leaf retypes,
    constants materialized as const_value, …) from *emitted* display formats
    are appended to `adjustments` as `(source, kind, detail)`.

    If *no* display format survives (every one was skipped), `UnsupportedFeature`
    is raised so the caller can treat the whole file as skipped.
    """
    context = descriptor.get("context") or {}
    contract = context.get("contract") or {}
    deployments = contract.get("deployments") or []
    display = descriptor.get("display") or {}
    formats = display.get("formats") or {}
    definitions = display.get("definitions") or {}
    constants = (descriptor.get("metadata") or {}).get("constants") or {}
    if not deployments:
        LOG.info("%s: no deployments, skipping", source)
        return []

    # Distinct unsupported features found in THIS file, for logging. Tracked at
    # file level (deduped), but skipping is decided per display format below.
    file_features: list[tuple[str, str]] = []
    seen_features: set[tuple[str, str]] = set()
    # Whether the display format currently being processed hit any unsupported
    # feature. Reset per signature below and flipped by note() on *every* call —
    # even one whose (feature, detail) was already seen in an earlier signature
    # and thus deduped out of file_features. The drop decision must not depend on
    # file_features growing, or a repeated feature would emit a broken format.
    had_issue = False

    def note(feature: str, detail: str) -> None:
        nonlocal had_issue
        had_issue = True
        LOG.info("%s: unsupported %s: %s", source, feature, detail)
        key = (feature, detail)
        if key not in seen_features:
            seen_features.add(key)
            file_features.append(key)

    # Candidates from clean display formats; expanded into records below.
    pending: list[tuple[str, str, list[ABIValue], list[ERC7730Field]]] = []

    for sig_key, display_format in formats.items():
        # A display format is dropped whole if it hits any unsupported feature;
        # note() flips this flag (even for a feature already seen in an earlier
        # signature, which file_features would dedupe away).
        had_issue = False

        if sig_key.startswith("0x"):
            # Hex selector — we can't derive parameter types without an ABI.
            note("selector-only-entry", sig_key)
            continue
        try:
            parsed: Function = parse_signature(sig_key)
        except Exception as e:
            note("unparseable-signature", f"{sig_key}: {e}")
            continue

        canonical = compute_signature(parsed)
        func_sig_hex = signature_to_selector(canonical)
        # Selector is our own 4-byte computation; guard the invariant.
        if len(func_sig_hex) != 10 or not _is_hex(func_sig_hex[2:]):
            LOG.warning("%s: skipping %s — bad selector %r", source, sig_key, func_sig_hex)
            continue
        inputs = list(parsed.inputs or [])

        try:
            parameter_definitions = [build_abi_value(p) for p in inputs]
        except UnsupportedFeature as e:
            note(e.feature, f"{sig_key}: {e.detail}")
            continue
        except ValueError as e:
            note("unrepresentable-params", f"{sig_key}: {e}")
            continue

        # Adjustments made while building THIS display format; merged into the
        # file-level output only if the format is actually emitted (a dropped
        # format's retypes/overrides die with its parameter_definitions).
        format_adjustments: Adjustments = []

        field_defs: list[ERC7730Field] = []
        for f in display_format.get("fields", []):
            if not isinstance(f, dict):
                note("malformed-field-entry", f"{sig_key}: {f!r}")
                continue
            resolved = _resolve_ref(f, definitions)
            if resolved is None:
                # Unresolvable `$ref` — the field's display info is unavailable,
                # so skip the file rather than silently drop a displayed field.
                note("unresolvable-ref", f"{sig_key}: {f.get('$ref')!r}")
                continue
            f = resolved
            if isinstance(f.get("fields"), list):
                # A nested field group (a `path` scoping sub-`fields`, e.g.
                # `#.marketParams` in Morpho Blue). The group itself has no
                # `format`, so it would otherwise be skipped as hidden — but its
                # sub-fields ARE displayed and we can't express their relative
                # paths, so drop the whole display format instead.
                note("nested-fields", f"{sig_key}: path {f.get('path')!r}")
                continue
            if "path" not in f:
                # A displayed field not bound to a calldata parameter: a
                # constant/text field (`format: raw` with a `value`, possibly a
                # $.metadata.constants.* reference) rides in the proto as a
                # const_value path. Hidden ones are skipped without checks.
                if not _field_is_displayed(f):
                    continue
                if "value" not in f:
                    note(
                        "non-path-field",
                        f"{sig_key}: {f.get('format')} (label {f.get('label')!r})",
                    )
                    continue
                label = f.get("label", "")
                if not label:
                    note("missing-label", f"{sig_key}: constant field {f.get('value')!r}")
                    continue
                try:
                    field_defs.append(
                        _build_const_value_field(
                            label, f["format"], f["value"], constants, format_adjustments
                        )
                    )
                except UnsupportedFeature as e:
                    note(e.feature, f"{sig_key}: {e.detail}")
                continue
            try:
                built = build_field_dict(
                    f,
                    inputs,
                    constants=constants,
                    label_context=f"{source}:{sig_key}",
                    parameter_definitions=parameter_definitions,
                    adjustments=format_adjustments,
                )
            except UnsupportedFeature as e:
                note(e.feature, f"{sig_key}: {e.detail}")
                continue
            if built is not None:
                field_defs.append(built)

        # Drop this display format whole if any field/param was unsupported —
        # never emit one with a field silently missing. Other formats survive.
        if had_issue:
            continue

        if adjustments is not None:
            adjustments.extend(
                (source, kind, f"{sig_key}: {detail}")
                for kind, detail in format_adjustments
            )
        intent = _get_intent(display_format)
        pending.append((func_sig_hex, intent, parameter_definitions, field_defs))

    # Log every feature found, even when some display formats were still emitted.
    if unsupported is not None:
        unsupported.extend((source, feat, detail) for feat, detail in file_features)

    # Nothing representable in the whole file — let the caller skip it.
    if not pending and file_features:
        raise UnsupportedFeature(
            "descriptor-skipped",
            f"{source}: {len(file_features)} unsupported feature(s)",
        )

    results: list[ERC20DisplayFormat] = []
    for func_sig_hex, intent, parameter_definitions, field_defs in pending:
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
