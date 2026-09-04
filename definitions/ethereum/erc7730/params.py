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

from ..types import ERC7730Field
from .erc7730 import (
    _FORMATTER_MAP,
    KIND_ADDRESS,
    KIND_BYTES,
    KIND_NUMERIC,
    UnsupportedFeature,
    _descriptor_path_parts,
    _FormatContext,
    _is_hex,
    _narrow_word_slice_to_address,
    _normalize_hex,
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


def apply_token_amount_params(
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
        elif tp_kind == KIND_BYTES and _narrow_word_slice_to_address(tp_path):
            # A 32-byte word slice (paraswap `#.data.[292:324]`): the token
            # address is the word's low 20 bytes.
            out["token_path"] = tp_path
            ctx.adjust(
                "address-in-word-slice",
                f"{token_path_str} slices a 32-byte word — narrowed to its "
                f"low 20 bytes (field {label!r})",
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
    if isinstance(threshold, str) and threshold.startswith("$."):
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


def apply_calldata_params(
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
    elif cp_kind == KIND_BYTES and _narrow_word_slice_to_address(cp_path):
        out["callee_path"] = cp_path
        ctx.adjust(
            "address-in-word-slice",
            f"{callee_str} slices a 32-byte word — narrowed to its low "
            f"20 bytes (field {label!r})",
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


def apply_enum_params(
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
        raise UnsupportedFeature("unresolvable-enum-ref", f"{ref!r} (field {label!r})")

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
