"""Per-formatter `params` handling for calldata-bound display fields.

Split out of `erc7730.py` section 4c: one `apply_*_params` helper per
formatter that takes a `params` dict, shared `_FormatContext` and (already
built) `out` field dict, and fills in the formatter-specific proto keys —
`_build_path_field` (still in `erc7730.py`) dispatches to these by `fmt`.
Also holds the token/native-currency address helpers only `tokenAmount`
needs, which moved here with their one caller.
"""

from __future__ import annotations

from typing import Any

from ..types import ERC7730Field, ERC7730Path
from .erc7730 import (
    _FORMATTER_MAP,
    KIND_ADDRESS,
    KIND_BYTES,
    KIND_NUMERIC,
    UnsupportedFeature,
    _abi_leaf_at,
    _descriptor_path_parts,
    _FormatContext,
    _is_hex,
    _narrow_word_slice_to_address,
    _nominal_slice_length,
    _normalize_hex,
    _parse_int,
    _path_slice,
    _resolve_constant,
    _retype_uint_leaf_as_address,
    path_to_dict,
)

_HEX_DIGITS = frozenset("0123456789abcdef")


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


def _resolve_native_currency_addresses(
    params: dict[str, Any], label: str, constants: dict[str, Any]
) -> list[str]:
    """Resolve `nativeCurrencyAddress` entries to normalized 20-byte hex.

    Entries may be literals or `$.metadata.constants.*` references. The
    result is passed through as `out["native_currency_address"]` — the
    firmware compares the resolved token address (const or calldata-walked)
    against it at render time and renders a match as plain AMOUNT.
    """
    raw = params.get("nativeCurrencyAddress")
    if raw is None:
        return []
    addresses = []
    for entry in raw if isinstance(raw, list) else [raw]:
        resolved = _resolve_address_ref(entry, constants)
        if resolved is None:
            raise UnsupportedFeature(
                "invalid-native-currency-address", f"{entry!r} (field {label!r})"
            )
        addresses.append(resolved)
    return addresses


def _reject_conflicting(
    params: dict[str, Any], const_key: str, path_key: str, label: str
) -> None:
    """DROP a field setting both a constant param and its `*Path` twin.

    The spec makes every such pair mutually exclusive (`not required: [a, b]`),
    and picking one silently would render something the author didn't write.
    """
    if params.get(const_key) is not None and params.get(path_key) is not None:
        raise UnsupportedFeature(
            "conflicting-params", f"both {const_key} and {path_key} (field {label!r})"
        )


def _resolve_const_address(
    value: Any, param: str, label: str, ctx: _FormatContext
) -> str:
    """Resolve a constant address param (`token`, `callee`, `spender`)."""
    resolved = _resolve_address_ref(value, ctx.constants)
    if resolved is None:
        raise UnsupportedFeature(
            f"invalid-const-{param}", f"{value!r} (field {label!r})"
        )
    return resolved


def _resolve_address_path(
    path_str: str, what: str, label: str, ctx: _FormatContext
) -> ERC7730Path:
    """Resolve a `*Path` param that must point at an address.

    Shared by `tokenPath`, `calleePath` and `spenderPath`. Besides a plain
    address leaf, two packed-address patterns are accepted (and logged):
    a uint leaf retyped to address (section 4b, e.g. 1inch
    `order.takerAsset`), and a 32-byte word slice narrowed to its low 20
    bytes (paraswap `#.data.[292:324]`).
    """
    try:
        path, kind = path_to_dict(path_str, ctx.inputs)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(
            f"unresolvable-{what}-path",
            f"{path_str}: [{e.feature}] {e.detail} (field {label!r})",
        ) from None
    if kind == KIND_ADDRESS:
        return path
    if kind == KIND_NUMERIC and _retype_uint_leaf_as_address(
        ctx.parameter_definitions, path
    ):
        ctx.adjust(
            f"{what}-address-in-numeric",
            f"{path_str} is numeric but used as {what} address — "
            f"ABI leaf retyped to address (field {label!r})",
        )
        return path
    if kind == KIND_BYTES and _narrow_word_slice_to_address(path):
        ctx.adjust(
            "address-in-word-slice",
            f"{path_str} slices a 32-byte word — narrowed to its low "
            f"20 bytes (field {label!r})",
        )
        return path
    raise UnsupportedFeature(
        f"unresolvable-{what}-path",
        f"{path_str} is {kind}, not an address (field {label!r})",
    )


def _resolve_numeric_path(
    path_str: str, what: str, label: str, ctx: _FormatContext
) -> ERC7730Path:
    """Resolve a `*Path` param that must point at an integer (or `@.value` /
    `@.chainId`). Shared by `chainIdPath` and `amountPath`."""
    try:
        path, kind = path_to_dict(path_str, ctx.inputs)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(
            f"unresolvable-{what}-path",
            f"{path_str}: [{e.feature}] {e.detail} (field {label!r})",
        ) from None
    if kind != KIND_NUMERIC:
        raise UnsupportedFeature(
            f"unresolvable-{what}-path",
            f"{path_str} is {kind}, not numeric (field {label!r})",
        )
    return path


def _resolve_const_int(
    raw: Any, what: str, label: str, ctx: _FormatContext, lo: int, hi: int
) -> int:
    """Resolve a constant integer param (`chainId`, `amount`) into [lo, hi).

    `raw` is a JSON integer, a decimal / `0x`-hex string, or a
    `$.metadata.constants.*` reference to one.
    """
    value = raw
    if isinstance(raw, str) and raw.startswith("$."):
        value = _resolve_constant(raw, ctx.constants)
        if value is None:
            raise UnsupportedFeature(f"unresolvable-{what}", f"{raw} (field {label!r})")
    n = None if isinstance(value, bool) else _parse_int(value)
    if n is None or not lo <= n < hi:
        raise UnsupportedFeature(f"invalid-{what}", f"{raw!r} (field {label!r})")
    return n


def _resolve_selector_path(
    path_str: str, label: str, ctx: _FormatContext
) -> ERC7730Path:
    """Resolve `selectorPath`: a `bytes4` leaf, or any slice of exactly 4 bytes."""
    try:
        path, _kind = path_to_dict(path_str, ctx.inputs)
    except UnsupportedFeature as e:
        raise UnsupportedFeature(
            "unresolvable-selector-path",
            f"{path_str}: [{e.feature}] {e.detail} (field {label!r})",
        ) from None
    slice_ = _path_slice(path)
    if slice_ is not None:
        if _nominal_slice_length(*slice_) == 4:
            return path
    elif (_abi_leaf_at(ctx.parameter_definitions, path) or {}).get(
        "atomic"
    ) == "ABI_BYTES4":
        return path
    raise UnsupportedFeature(
        "unresolvable-selector-path",
        f"{path_str} is not a 4-byte selector (field {label!r})",
    )


def _apply_chain_id(
    out: ERC7730Field, params: dict[str, Any], label: str, ctx: _FormatContext
) -> None:
    """Fill in the token's chain from `chainId` / `chainIdPath`.

    Used by `tokenAmount` and `tokenTicker` for cross-chain swaps, where the
    token lives on a chain other than the transaction's. `chainIdPath` becomes
    `chain_id_path` (a numeric calldata leaf, or `@.chainId`); `chainId` — a
    literal or `$.metadata.constants.*` reference — becomes `const_chain_id`
    (proto uint64, so 1..2**64-1). Absent both, the firmware uses the
    transaction's chain.
    """
    _reject_conflicting(params, "chainId", "chainIdPath", label)
    if params.get("chainIdPath") is not None:
        out["chain_id_path"] = _resolve_numeric_path(
            str(params["chainIdPath"]), "chain-id", label, ctx
        )
        return
    if params.get("chainId") is not None:
        out["const_chain_id"] = _resolve_const_int(
            params["chainId"], "chain-id", label, ctx, 1, 2**64
        )


def apply_token_amount_params(
    out: ERC7730Field,
    params: dict[str, Any],
    label: str,
    ctx: _FormatContext,
) -> None:
    """Fill in FORMATTER_TOKEN_AMOUNT's token reference and threshold.

    A descriptor names the token one of three ways, tried in order:

      1. `tokenPath`  — the token's address is in calldata; emitted as a
         proto `token_path` the firmware walks.
      2. `token`      — a hardcoded address (or constants reference);
         emitted as `const_token_address`.
      3. neither      — an unknown token: emitted with NO token reference.

    `chainId` / `chainIdPath` name the token's chain (see `_apply_chain_id`).
    """

    def _apply_threshold() -> None:
        """Normalize a `threshold` param to hex bytes; reject the unserializable."""
        threshold = params.get("threshold")
        if threshold is None:
            return
        if isinstance(threshold, str) and threshold.startswith("$."):
            resolved = _resolve_constant(threshold, ctx.constants)
            if resolved is None:
                raise UnsupportedFeature(
                    "unresolvable-threshold", f"{threshold} (field {label!r})"
                )
            threshold = resolved
        if isinstance(threshold, str):
            normalized = _normalize_hex(threshold)
            # `_normalize_hex` doesn't validate: a non-hex value would slip
            # through and crash `bytes.fromhex` at serialization time.
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

        message = params.get("message")
        if message is not None:
            out["threshold_message"] = str(message)

    _reject_conflicting(params, "token", "tokenPath", label)
    native_addresses = _resolve_native_currency_addresses(params, label, ctx.constants)

    if params.get("tokenPath") is not None:
        out["token_path"] = _resolve_address_path(
            str(params["tokenPath"]), "token", label, ctx
        )
    elif params.get("token") is not None:
        const_addr = _resolve_const_address(params["token"], "token", label, ctx)
        if int(const_addr, 16) == 0 and const_addr not in native_addresses:
            # Bad descriptor but spec allows. Good to log.
            ctx.adjust(
                "null-token-not-native",
                f"token is the zero address but not in nativeCurrencyAddress "
                f"— renders as an unknown token (field {label!r})",
            )
        out["const_token_address"] = const_addr
    else:
        ctx.adjust(
            "tokenamount-unknown-token",
            f"no tokenPath/token — renders as an unknown token (field {label!r})",
        )

    if native_addresses:
        out["native_currency_address"] = native_addresses

    _apply_chain_id(out, params, label, ctx)
    _apply_threshold()


def apply_address_name_params(
    out: ERC7730Field, params: dict[str, Any], label: str, ctx: _FormatContext
) -> None:
    """Flag addressName fields whose `types` ask for a contract/token name.

    The device only needs one bit: when `types` includes `contract` or
    `token` it asks the host for a known name for the address; otherwise it
    shows the plain address. `sources` (`local`, `ens`) and the other types
    (`eoa`, `wallet`, `collection`) have no on-device counterpart, so they are
    ignored. The flag is only emitted when true (the proto default is false).
    """
    types = params.get("types")
    if types is None:
        return
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise UnsupportedFeature(
            "invalid-address-types", f"{types!r} (field {label!r})"
        )
    if any(t in ("contract", "token") for t in types):
        out["lookup_address"] = True


def apply_token_ticker_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Render a `tokenTicker` field as addressName with a name lookup.

    The spec's only params, `chainId` / `chainIdPath`, name the chain the
    token lives on (cross-chain swaps); they ride on the field as
    `const_chain_id` / `chain_id_path` so the name is looked up on that chain.
    """
    _apply_chain_id(out, params, label, ctx)
    out["lookup_address"] = True
    ctx.adjust(
        "token-ticker-as-address-name",
        f"{path_str}: tokenTicker rendered as addressName with name lookup "
        f"(field {label!r})",
    )


def apply_unit_params(
    out: ERC7730Field, params: dict[str, Any], label: str, ctx: _FormatContext
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
    base = params.get("base")
    if base:
        if isinstance(base, str) and base.startswith("$."):
            resolved = _resolve_constant(base, ctx.constants)
            if resolved is None:
                raise UnsupportedFeature(
                    "unresolvable-constant-value", f"{base!r} (field {label!r})"
                )
            base = resolved
        out["base"] = str(base)
    if params.get("prefix") is not None:
        out["prefix"] = bool(params["prefix"])


def apply_date_params(
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


_CALLDATA_PARAMS = frozenset(
    {
        "callee",
        "calleePath",
        "selector",
        "selectorPath",
        "amount",
        "amountPath",
        "spender",
        "spenderPath",
    }
)


def apply_calldata_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Fill in FORMATTER_CALLDATA's callee, selector and inner container values.

    The embedded calldata is rendered with the *called contract's* display
    format, so the callee is required: `calleePath` -> `callee_path`, or a
    constant `callee` -> `const_callee`. When the embedded blob is args-only,
    the nested function's 4-byte selector comes from `selector` (constant)
    or `selectorPath` -> `selector_path` (a `bytes4` leaf or 4-byte slice).

    The inner call's container values: `amountPath` -> `amount_path` / constant
    `amount` -> `const_amount` (inner `@.value`; minimal big-endian uint256,
    like `threshold`), `spenderPath` -> `spender_path` / constant `spender` ->
    `const_spender` (inner `@.from`).
    """
    for const_key in ("callee", "selector", "amount", "spender"):
        _reject_conflicting(params, const_key, f"{const_key}Path", label)

    if params.get("calleePath"):
        out["callee_path"] = _resolve_address_path(
            str(params["calleePath"]), "callee", label, ctx
        )
    elif params.get("callee") is not None:
        out["const_callee"] = _resolve_const_address(
            params["callee"], "callee", label, ctx
        )
    else:
        raise UnsupportedFeature(
            "calldata-missing-callee", f"{path_str} (field {label!r})"
        )

    selector = params.get("selector")
    if params.get("selectorPath") is not None:
        out["selector_path"] = _resolve_selector_path(
            str(params["selectorPath"]), label, ctx
        )
    elif selector is not None:
        normalized = _normalize_hex(str(selector))
        if len(normalized) != 8 or not _is_hex(normalized):
            raise UnsupportedFeature(
                "invalid-calldata-selector", f"{selector!r} (field {label!r})"
            )
        out["selector"] = normalized

    if params.get("amountPath") is not None:
        out["amount_path"] = _resolve_numeric_path(
            str(params["amountPath"]), "amount", label, ctx
        )
    elif params.get("amount") is not None:
        amount = _resolve_const_int(params["amount"], "amount", label, ctx, 0, 2**256)
        out["const_amount"] = _normalize_hex(hex(amount))

    if params.get("spenderPath") is not None:
        out["spender_path"] = _resolve_address_path(
            str(params["spenderPath"]), "spender", label, ctx
        )
    elif params.get("spender") is not None:
        out["const_spender"] = _resolve_const_address(
            params["spender"], "spender", label, ctx
        )

    ignored = sorted(set(params) - _CALLDATA_PARAMS)
    if ignored:
        ctx.adjust(
            "calldata-params-ignored",
            f"{path_str}: {', '.join(ignored)} have no proto representation "
            f"(field {label!r})",
        )


# EthereumERC7730EnumEntry.key value reserved for "the real key is in wide_key"
_ENUM_WIDE_KEY = 0xFFFFFFFF


def apply_enum_params(
    out: ERC7730Field,
    params: dict[str, Any],
    path_str: str,
    label: str,
    ctx: _FormatContext,
) -> None:
    """Resolve an enum field's `$.metadata.enums.*` reference into enum_values.

    The descriptor maps calldata keys to display strings, e.g.
    `{"0": "Stable", "1": "Variable"}`. The JSON-boolean variant
    (`True`/`False` keys over a bool calldata value) maps to 1/0 and is logged
    as an adjustment. Every accepted enum field is logged too, so the
    feature's use is visible while firmware support is fresh.

    Keys below 0xFFFFFFFF ride in the proto's uint32 `key`. Wider keys
    (0xFFFFFFFF included, up to 32 bytes) set `key` to the reserved
    0xFFFFFFFF and carry the real value in `wide_key` (minimal big-endian).
    
    NOTE: An enum with any wide key also gets one trailing guard entry
    `{key: 0xFFFFFFFF, value: ""}`. The duplication blocks older firmware from
    misinterpreting the "key" if 0xFFFFFFFF was a valid value, as it rejects
    enums with duplicate keys.
    TODO: key will be deprecated in favour of wide_key later
    https://github.com/trezor/trezor-firmware/issues/8337
    """
    ref = params.get("$ref")
    if not ref:
        raise UnsupportedFeature("enum-missing-ref", f"{path_str} (field {label!r})")
    parts = _descriptor_path_parts(str(ref))
    entries = None
    if parts is not None and parts[:2] == ("metadata", "enums"):
        entries = ctx.enums.get(parts[2])
    if not isinstance(entries, dict) or not entries:
        raise UnsupportedFeature("unresolvable-enum-ref", f"{ref!r} (field {label!r})")

    values: list[dict[str, Any]] = []
    seen: dict[int, Any] = {}
    bool_keys = False
    wide_count = 0
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
        if key < 0:
            # No good reason to have negative keys. Impossible with byte indexing now.
            raise UnsupportedFeature(
                "invalid-enum-entry",
                f"negative key {k!r} in {ref!r} (field {label!r})",
            )
        if key in seen:
            raise UnsupportedFeature(
                "invalid-enum-entry",
                f"keys {seen[key]!r} and {k!r} are the same value in {ref!r} "
                f"(field {label!r})",
            )
        seen[key] = k
        if key < _ENUM_WIDE_KEY:
            values.append({"key": key, "value": str(v)})
            continue
        wide = key.to_bytes((key.bit_length() + 7) // 8, "big")
        if len(wide) > 32:
            raise UnsupportedFeature(
                "invalid-enum-entry",
                f"key {k!r} wider than 32 bytes in {ref!r} (field {label!r})",
            )
        values.append({"key": _ENUM_WIDE_KEY, "wide_key": wide.hex(), "value": str(v)})
        wide_count += 1

    if bool_keys:
        ctx.adjust(
            "enum-bool-keys",
            f"{ref!r}: True/False keys mapped to 1/0 (field {label!r})",
        )
    if wide_count:
        ctx.adjust(
            "enum-wide-keys",
            f"{ref!r}: {wide_count} key(s) >= 0xFFFFFFFF carried in wide_key, "
            f"guard entry appended (field {label!r})",
        )
    ctx.adjust(
        "enum-field",
        f"{path_str}: {len(values)} value(s) from {ref!r} (field {label!r})",
    )
    if wide_count:
        # see the docstring: old firmware must see a duplicate 0xFFFFFFFF key
        values.append({"key": _ENUM_WIDE_KEY, "value": ""})
    out["enum_values"] = values
