from typing import Any

import pytest
from erc7730.common.abi import parse_signature
from erc7730.model.abi import Component

from .erc7730 import (
    KIND_ADDRESS,
    KIND_BYTES,
    KIND_NUMERIC,
    KIND_OTHER,
    UnsupportedFeature,
    _resolve_ref,
    build_abi_value,
    build_display_formats,
    path_to_dict,
)


def _component(signature: str) -> Component:
    """Parse a single-parameter signature into its one input component."""
    return list(parse_signature(signature).inputs or [])[0]


def _inputs(signature: str) -> list[Component]:
    """Parse a function signature into its list of input components."""
    return list(parse_signature(signature).inputs or [])


def _descriptor(
    formats: dict[str, Any],
    definitions: dict[str, Any] | None = None,
    constants: dict[str, Any] | None = None,
    deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal (post-includes) ERC-7730 descriptor with one deployment."""
    display: dict[str, Any] = {"formats": formats}
    if definitions is not None:
        display["definitions"] = definitions
    if deployments is None:
        deployments = [{"chainId": 1, "address": "0x" + "11" * 20}]
    return {
        "context": {"contract": {"deployments": deployments}},
        "display": display,
        "metadata": {"constants": constants or {}},
    }


# =====================================================================
#                       path_to_dict — leaf kinds
# =====================================================================


def test_path_to_dict_address():
    assert path_to_dict("recipient", _inputs("f(address recipient)")) == (
        {"path": [0]},
        KIND_ADDRESS,
    )


def test_path_to_dict_numeric():
    assert path_to_dict("amount", _inputs("f(uint256 amount)")) == (
        {"path": [0]},
        KIND_NUMERIC,
    )


def test_path_to_dict_bytes_and_string_and_bool_are_kind_bytes():
    # Scalar bytes/string/bool leaves are renderable only by `raw`.
    assert path_to_dict("data", _inputs("f(bytes data)")) == ({"path": [0]}, KIND_BYTES)
    assert path_to_dict("s", _inputs("f(string s)")) == ({"path": [0]}, KIND_BYTES)
    assert path_to_dict("ok", _inputs("f(bool ok)")) == ({"path": [0]}, KIND_BYTES)
    assert path_to_dict("h", _inputs("f(bytes32 h)")) == ({"path": [0]}, KIND_BYTES)


def test_path_to_dict_into_tuple():
    inputs = _inputs("swap((address srcToken, uint256 amount) desc)")
    assert path_to_dict("desc.srcToken", inputs) == ({"path": [0, 0]}, KIND_ADDRESS)
    assert path_to_dict("desc.amount", inputs) == ({"path": [0, 1]}, KIND_NUMERIC)


def test_path_to_dict_array_element_peels_one_dimension():
    # Indexing a uint256[] yields a uint256 scalar.
    assert path_to_dict("pools.[-1]", _inputs("f(uint256[] pools)")) == (
        {"path": [0, -1]},
        KIND_NUMERIC,
    )
    # Indexing an address[] yields an address scalar.
    assert path_to_dict("xs.[0]", _inputs("f(address[] xs)")) == (
        {"path": [0, 0]},
        KIND_ADDRESS,
    )


def test_path_to_dict_unindexed_array_is_other():
    # A whole, un-indexed array is not a scalar — no formatter can render it.
    assert path_to_dict("xs", _inputs("f(address[] xs)")) == (
        {"path": [0]},
        KIND_OTHER,
    )


def test_path_to_dict_container_paths():
    assert path_to_dict("@.from", []) == ({"container_path": "FROM"}, KIND_ADDRESS)
    assert path_to_dict("@.to", []) == ({"container_path": "TO"}, KIND_ADDRESS)
    assert path_to_dict("@.value", []) == ({"container_path": "VALUE"}, KIND_NUMERIC)


def test_path_to_dict_unsupported_returns_none():
    # Descriptor (constant) path, array slice, and unknown name are all
    # skipped silently (the rest of the descriptor still builds).
    assert path_to_dict("$.metadata.constants.x", []) is None
    assert path_to_dict("data.[0:20]", _inputs("f(bytes data)")) is None
    assert path_to_dict("nope", _inputs("f(uint256 amount)")) is None


def test_path_to_dict_unsupported_container_raises():
    # Unsupported @. paths reference transaction-level fields — fail loudly.
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict("@.gas", [])
    assert exc.value.feature == "unsupported-container-path"
    with pytest.raises(UnsupportedFeature):
        path_to_dict("@.nonce", [])


# =====================================================================
#                       _resolve_ref — $ref merging
# =====================================================================


def test_resolve_ref_noop_without_ref():
    field = {"path": "amount", "format": "amount"}
    assert _resolve_ref(field, {}) is field


def test_resolve_ref_unknown_definition_returns_none():
    # A dangling `$ref` is unresolvable — the field's display info is missing,
    # so return None (caller skips) rather than the field as-is.
    field = {"path": "amount", "$ref": "$.display.definitions.missing"}
    assert _resolve_ref(field, {}) is None


def test_resolve_ref_unsupported_ref_returns_none():
    field = {"path": "amount", "$ref": "$.metadata.enums.something"}
    assert _resolve_ref(field, {}) is None


def test_resolve_ref_field_overrides_definition():
    field = {"path": "amount", "$ref": "$.display.definitions.x", "label": "Override"}
    defs = {"x": {"label": "Default", "format": "amount"}}
    merged = _resolve_ref(field, defs)
    assert merged["label"] == "Override"
    assert merged["format"] == "amount"
    assert "$ref" not in merged


def test_resolve_ref_deep_merges_params():
    field = {
        "path": "amount",
        "$ref": "$.display.definitions.x",
        "params": {"tokenPath": "srcToken"},
    }
    defs = {
        "x": {
            "label": "L",
            "format": "tokenAmount",
            "params": {"nativeCurrencyAddress": ["0xabc"]},
        }
    }
    merged = _resolve_ref(field, defs)
    # Definition keys are preserved; field keys are added on top.
    assert merged["params"] == {
        "nativeCurrencyAddress": ["0xabc"],
        "tokenPath": "srcToken",
    }


def test_unresolvable_ref_field_skips_file():
    # A field whose display info comes from a missing definition must skip the
    # file, not be silently dropped (no `definitions` provided here).
    desc = _descriptor(
        formats={
            "f(uint256 amount)": {
                "fields": [
                    {"$ref": "$.display.definitions.missing", "path": "amount"}
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"unresolvable-ref"}


# =====================================================================
#         build_display_formats — formatter ↔ type compatibility
# =====================================================================


_ADDR_FIELD = {"f(address x)": {"fields": [{"path": "x", "label": "L", "format": "addressName"}]}}

# A bespoke `format` value that is deliberately not a real ERC-7730 formatter, so
# tests that just need "some unsupported formatter" don't silently go stale if we
# later add support for a real one (e.g. `date`, `enum`). `_field_is_displayed`
# only checks that a `format` is present, and `build_field_dict` rejects anything
# absent from `_FORMATTER_MAP`, so this routes through the `unsupported-formatter`
# path exactly like a real-but-unsupported name would.
_UNSUPPORTED_FORMAT = "notAFormatter"


def test_record_shape():
    [rec] = build_display_formats(_descriptor(formats=_ADDR_FIELD))
    assert rec["chain_id"] == 1
    assert rec["address"] == "0x" + "11" * 20
    assert rec["func_sig"].startswith("0x") and len(rec["func_sig"]) == 10
    assert len(rec["field_definitions"]) == 1


# --- record sanity guards (#3 address, #4 chain_id, #5 selector) ---


def test_deployment_with_invalid_address_is_skipped():
    # One good deployment, one with a non-hex address — only the good one emits.
    desc = _descriptor(
        formats=_ADDR_FIELD,
        deployments=[
            {"chainId": 1, "address": "0x" + "11" * 20},
            {"chainId": 10, "address": "0x" + "z" * 40},  # right length, not hex
        ],
    )
    recs = build_display_formats(desc)
    assert [r["chain_id"] for r in recs] == [1]


def test_deployment_with_bad_address_length_is_skipped():
    desc = _descriptor(
        formats=_ADDR_FIELD,
        deployments=[{"chainId": 1, "address": "0x1234"}],
    )
    assert build_display_formats(desc) == []


def test_deployment_with_non_positive_chain_id_is_skipped():
    desc = _descriptor(
        formats=_ADDR_FIELD,
        deployments=[
            {"chainId": 0, "address": "0x" + "22" * 20},
            {"chainId": -1, "address": "0x" + "33" * 20},
            {"chainId": 5, "address": "0x" + "44" * 20},
        ],
    )
    recs = build_display_formats(desc)
    assert [r["chain_id"] for r in recs] == [5]


def test_deployment_with_non_integer_chain_id_is_skipped():
    desc = _descriptor(
        formats=_ADDR_FIELD,
        deployments=[{"chainId": "mainnet", "address": "0x" + "55" * 20}],
    )
    assert build_display_formats(desc) == []


def test_addressname_on_address_is_kept():
    desc = _descriptor(
        formats={"f(address x)": {"fields": [{"path": "x", "label": "L", "format": "addressName"}]}}
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"


def test_addressname_on_uint_skips_file():
    desc = _descriptor(
        formats={"f(uint256 x)": {"fields": [{"path": "x", "label": "L", "format": "addressName"}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert [(feat, _det) for _src, feat, _det in unsupported] == [
        ("formatter-type-mismatch", unsupported[0][2])
    ]


def test_tokenamount_no_token_skips_file():
    # tokenAmount on a numeric value with no token reference at all: the token is
    # unknown and FORMATTER_TOKEN_AMOUNT is unconstructable on-device, so skip.
    desc = _descriptor(
        formats={"f(uint256 x)": {"fields": [{"path": "x", "label": "L", "format": "tokenAmount"}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"tokenamount-unknown-token"}


def test_tokenamount_on_bytes_skips_file():
    desc = _descriptor(
        formats={"f(bytes x)": {"fields": [{"path": "x", "label": "L", "format": "tokenAmount"}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_tokenpath_address_is_kept():
    desc = _descriptor(
        formats={
            "f(uint256 amount, address token)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "token"},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["token_path"] == {"path": [1]}


def test_tokenpath_non_address_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 amount, uint256 notToken)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "notToken"},
                    }
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"unresolvable-token-path"}


def _const_token_desc(token, constants=None):
    return _descriptor(
        formats={
            "f(uint256 amount)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"token": token},
                    }
                ]
            }
        },
        constants=constants,
    )


def test_tokenamount_literal_token_is_const_token_address():
    # A literal `token` address is emitted directly as const_token_address.
    [rec] = build_display_formats(_const_token_desc("0x" + "ab" * 20))
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    assert field["const_token_address"] == "ab" * 20
    assert "token_path" not in field


def test_tokenamount_constant_ref_token_is_resolved():
    # The token may be a $.metadata.constants.* reference resolved by the parser.
    desc = _const_token_desc(
        "$.metadata.constants.usdc", constants={"usdc": "0x" + "cd" * 20}
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["const_token_address"] == "cd" * 20


def test_tokenamount_invalid_const_token_skips_file():
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(_const_token_desc("not-an-address"), unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"invalid-const-token"}


def test_tokenamount_unresolvable_constant_token_skips_file():
    # A $.metadata.constants.* reference with no matching constant is invalid.
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _const_token_desc("$.metadata.constants.missing"), unsupported=unsupported
        )
    assert {feat for _src, feat, _det in unsupported} == {"invalid-const-token"}


def test_tokenamount_literal_zero_token_with_native_is_amount():
    # A literal zero token is the null/native token: native AMOUNT when declared.
    desc = _descriptor(
        formats={
            "f(uint256 amount)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {
                            "token": "0x" + "00" * 20,
                            "nativeCurrencyAddress": ["0x" + "00" * 20],
                        },
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_AMOUNT"
    assert "const_token_address" not in field


def test_tokenamount_literal_zero_token_without_native_skips_file():
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(_const_token_desc("0x" + "00" * 20), unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"tokenamount-unknown-token"}


def test_tokenamount_const_token_keeps_threshold():
    desc = _descriptor(
        formats={
            "f(uint256 amount)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"token": "0x" + "ab" * 20, "threshold": "0xff"},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["const_token_address"] == "ab" * 20
    assert field["threshold"] == "ff"


def test_const_token_address_serializes_to_proto():
    # The const_token_address hex makes it onto the proto field as raw bytes.
    from .common import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": {"path": [0]},
            "label": "Amt",
            "formatter": "FORMATTER_TOKEN_AMOUNT",
            "const_token_address": "ab" * 20,
        }
    )
    assert info.const_token_address == bytes.fromhex("ab" * 20)


def _tokenamount_native_desc(native_addresses, constants=None):
    return _descriptor(
        formats={
            "f(uint256 amount)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Min out",
                        "format": "tokenAmount",
                        "params": {"nativeCurrencyAddress": native_addresses},
                    }
                ]
            }
        },
        constants=constants,
    )


def test_tokenamount_no_token_with_null_native_is_amount():
    # No tokenPath: the token defaults to the null address. nativeCurrencyAddress
    # lists the zero address, so this is a native amount -> AMOUNT formatter, no
    # token_path (e.g. lifi ...ToNative "Minimum to receive"). FORMATTER_TOKEN_AMOUNT
    # without a token is meaningless and unconstructable on-device.
    desc = _tokenamount_native_desc(["0x" + "ee" * 20, "0x" + "00" * 20])
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_AMOUNT"
    assert "token_path" not in field


def test_tokenamount_no_token_via_constant_null_is_amount():
    # The zero-address sentinel may be a $.metadata.constants.* reference.
    desc = _tokenamount_native_desc(
        ["$.metadata.constants.addressAsNull"],
        constants={"addressAsNull": "0x" + "00" * 20},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_AMOUNT"


def test_tokenamount_no_token_without_null_native_skips_file():
    # No tokenPath and no zero-address sentinel -> the null token isn't declared
    # native, so it's an unknown token we can't represent -> skip.
    desc = _tokenamount_native_desc(["0x" + "ee" * 20])
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"tokenamount-unknown-token"}


def test_container_value_token_amount_native_is_amount():
    # @.value is numeric (kind-compatible with tokenAmount). With no token but the
    # zero address declared native, it resolves to a native AMOUNT on the VALUE path.
    desc = _descriptor(
        formats={
            "f()": {
                "fields": [
                    {
                        "path": "@.value",
                        "label": "Sent",
                        "format": "tokenAmount",
                        "params": {"nativeCurrencyAddress": ["0x" + "00" * 20]},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == {"container_path": "VALUE"}
    assert field["formatter"] == "FORMATTER_AMOUNT"


def test_container_value_rejects_address_name():
    desc = _descriptor(
        formats={"f()": {"fields": [{"path": "@.value", "label": "X", "format": "addressName"}]}}
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


# --- label / decimals sanity (#8) ---


def test_empty_label_on_displayed_field_skips_file():
    desc = _descriptor(
        formats={"f(address x)": {"fields": [{"path": "x", "label": "", "format": "addressName"}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"missing-label"}


def test_missing_label_on_displayed_field_skips_file():
    desc = _descriptor(
        formats={"f(address x)": {"fields": [{"path": "x", "format": "addressName"}]}}
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


def test_empty_label_on_hidden_field_is_fine():
    # Hidden fields are never shown, so a missing label must not skip the file.
    desc = _descriptor(
        formats={
            "f(address to, bytes raw)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {"path": "raw", "visible": "never"},  # no label, hidden
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert len(rec["field_definitions"]) == 1


def _threshold_desc(threshold):
    return _descriptor(
        formats={
            "f(uint256 amount, address token)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "token", "threshold": threshold},
                    }
                ]
            }
        }
    )


def test_tokenamount_valid_string_threshold_is_kept():
    [rec] = build_display_formats(_threshold_desc("0xff"))
    assert rec["field_definitions"][0]["threshold"] == "ff"


def test_tokenamount_int_threshold_is_kept():
    [rec] = build_display_formats(_threshold_desc(255))
    assert rec["field_definitions"][0]["threshold"] == "ff"


def test_tokenamount_non_hex_threshold_skips_file():
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(_threshold_desc("xyz"), unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"invalid-threshold"}


def test_tokenamount_negative_threshold_skips_file():
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(_threshold_desc(-1), unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"invalid-threshold"}


def test_unit_valid_decimals_is_kept():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x", "label": "Gas", "format": "unit", "params": {"decimals": 9}}]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["decimals"] == 9


def test_unit_negative_decimals_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x", "label": "Gas", "format": "unit", "params": {"decimals": -1}}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"invalid-decimals"}


def test_unit_non_numeric_decimals_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x", "label": "Gas", "format": "unit", "params": {"decimals": "nine"}}]
            }
        }
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


# =====================================================================
#                       raw / date formatters
# =====================================================================


def _single_field_desc(signature, field):
    return _descriptor(formats={signature: {"fields": [field]}})


@pytest.mark.parametrize(
    "signature",
    [
        "f(uint256 x)",
        "f(address x)",
        "f(bytes x)",
        "f(bytes32 x)",
        "f(string x)",
        "f(bool x)",
    ],
)
def test_raw_renders_any_scalar_leaf(signature):
    # `raw` accepts every scalar leaf kind (numeric/address/bytes/string/bool).
    desc = _single_field_desc(signature, {"path": "x", "label": "L", "format": "raw"})
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_RAW"


def test_raw_on_whole_array_skips_file():
    # An un-indexed array is not a single value, so even `raw` can't render it.
    desc = _single_field_desc(
        "f(uint256[] xs)", {"path": "xs", "label": "L", "format": "raw"}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_date_default_encoding_is_date_formatter():
    desc = _single_field_desc(
        "f(uint256 t)", {"path": "t", "label": "Deadline", "format": "date"}
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_DATE"


def test_date_timestamp_encoding_is_date_formatter():
    desc = _single_field_desc(
        "f(uint256 t)",
        {"path": "t", "label": "Deadline", "format": "date", "params": {"encoding": "timestamp"}},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_DATE"


def test_date_blockheight_encoding_falls_back_to_raw():
    # A block number is not a time — render it as a plain integer, not a date.
    desc = _single_field_desc(
        "f(uint256 b)",
        {"path": "b", "label": "Block", "format": "date", "params": {"encoding": "blockheight"}},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_RAW"


def test_date_on_non_numeric_skips_file():
    desc = _single_field_desc(
        "f(address x)", {"path": "x", "label": "L", "format": "date"}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


# =====================================================================
#       build_display_formats — file skip, collection, hidden fields
# =====================================================================


def test_unsupported_formatter_skips_file_and_is_collected():
    desc = _descriptor(
        formats={"f(uint256 x)": {"fields": [{"path": "x", "label": "L", "format": _UNSUPPORTED_FORMAT}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, source="prov/file.json", unsupported=unsupported)
    assert unsupported[0][0] == "prov/file.json"
    assert unsupported[0][1] == "unsupported-formatter"


def test_raw_constant_field_skips_file():
    # A displayed field bound to a constant (no calldata `path`).
    desc = _descriptor(
        formats={"f(uint256 x)": {"fields": [{"label": "Summary", "format": "raw", "value": "hi"}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"non-path-field"}


def test_bad_format_is_skipped_clean_formats_kept():
    # First function is fine; the second has an unsupported formatter. Only the
    # bad display format is dropped — the good one is still emitted — and the
    # bad one's feature is recorded.
    desc = _descriptor(
        formats={
            "good(address x)": {"fields": [{"path": "x", "label": "Addr", "format": "addressName"}]},
            "bad(uint256 y)": {"fields": [{"path": "y", "label": "Y", "format": _UNSUPPORTED_FORMAT}]},
        }
    )
    unsupported: list = []
    recs = build_display_formats(desc, unsupported=unsupported)
    # One clean format x one deployment.
    assert len(recs) == 1
    assert recs[0]["field_definitions"][0]["label"] == "Addr"
    # The dropped format's feature is still logged.
    assert "unsupported-formatter" in {feat for _src, feat, _det in unsupported}


def test_duplicate_unsupported_feature_across_formats_skips_each():
    # Regression: two broken formats raise the *same* (feature, detail) — here an
    # identical unsupported formatter on an identically-labelled field. The first
    # seeds the deduped feature log; the second's note() finds it already present
    # and adds nothing, so a "did the feature log grow?" check would think the
    # second format stayed clean and emit it with its field silently dropped.
    # Each broken format must be skipped on its own; only the clean one survives.
    desc = _descriptor(
        formats={
            "good(address x)": {"fields": [{"path": "x", "label": "Addr", "format": "addressName"}]},
            "bad1(uint256 y)": {"fields": [{"path": "y", "label": "Amt", "format": _UNSUPPORTED_FORMAT}]},
            "bad2(uint256 z)": {"fields": [{"path": "z", "label": "Amt", "format": _UNSUPPORTED_FORMAT}]},
        }
    )
    recs = build_display_formats(desc)
    assert len(recs) == 1
    assert recs[0]["field_definitions"][0]["label"] == "Addr"


def test_all_formats_bad_skips_whole_file():
    # When no display format survives, the whole file is skipped (raises).
    desc = _descriptor(
        formats={
            "bad1(uint256 y)": {"fields": [{"path": "y", "label": "Y", "format": _UNSUPPORTED_FORMAT}]},
            "bad2(uint256 z)": {"fields": [{"path": "z", "label": "Z", "format": _UNSUPPORTED_FORMAT}]},
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {
        "unsupported-formatter",
    }


def test_all_unsupported_features_in_one_format_are_logged():
    # The fields loop keeps scanning after the first unsupported field, so a
    # single (doomed) display format logs *every* distinct feature it hits, not
    # just the first. Here one format has two displayed fields with two different
    # problems; both must be recorded.
    desc = _descriptor(
        formats={
            "f(address a, address b)": {
                "fields": [
                    {"path": "a", "label": "A", "format": _UNSUPPORTED_FORMAT},
                    {"path": "b", "label": "", "format": "addressName"},
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {
        "unsupported-formatter",  # field a's bespoke formatter
        "missing-label",  # field b's empty label
    }


def test_hidden_field_with_bad_path_does_not_skip_file():
    # `visible: never` / no-format fields are not displayed: their (here
    # unrepresentable, array-iterating) paths must not be validated at all.
    desc = _descriptor(
        formats={
            # tuple has a dynamic `bytes d` field so it's valid inside an array.
            "f(address to, (address t, bytes d) [] swaps)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {"path": "swaps.[].t", "label": "hidden", "visible": "never"},
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["label"] == "To"


def test_hidden_field_with_format_but_visible_never_is_skipped():
    # A field can carry a `format` yet be hidden via visible:never — still not
    # displayed, so an unsupported formatter on it must not skip the file.
    desc = _descriptor(
        formats={
            "f(address to, bytes raw)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {"path": "raw", "label": "R", "format": _UNSUPPORTED_FORMAT, "visible": "never"},
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert len(rec["field_definitions"]) == 1


# =====================================================================
#            build_display_formats — $ref end-to-end
# =====================================================================


def test_ref_resolves_definition_end_to_end():
    desc = _descriptor(
        formats={
            "swap(address srcToken, uint256 amount)": {
                "fields": [
                    {
                        "path": "amount",
                        "$ref": "$.display.definitions.sendAmount",
                        "params": {"tokenPath": "srcToken"},
                    }
                ]
            }
        },
        definitions={
            "sendAmount": {
                "label": "Amount to Send",
                "format": "tokenAmount",
                "params": {"nativeCurrencyAddress": ["0x" + "ee" * 20]},
            }
        },
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["label"] == "Amount to Send"
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    # tokenPath from the field merged with the definition and resolved to srcToken.
    assert field["token_path"] == {"path": [0]}


# =====================================================================
#                       build_abi_value — tuples & arrays
# =====================================================================


def test_build_abi_value_atomic_and_dynamic():
    assert build_abi_value(_component("f(uint256 x)")) == {"atomic": "ABI_UINT256"}
    assert build_abi_value(_component("f(bytes x)")) == {"dynamic": "ABI_BYTES"}
    assert build_abi_value(_component("f(uint256[] x)")) == {
        "array": {"atomic": "ABI_UINT256"}
    }


def test_build_abi_value_top_level_tuple_carries_dynamism():
    # A top-level tuple with a dynamic field is itself dynamic.
    v = build_abi_value(_component("f((address a, bytes data) t)"))
    assert v["tuple"]["is_dynamic"] is True
    # An all-static top-level tuple is static.
    v = build_abi_value(_component("f((address a, uint256 n) t)"))
    assert v["tuple"]["is_dynamic"] is False


def test_build_abi_value_dynamic_tuple_in_array_is_kept_static():
    # In-array tuples are always emitted as static (the array layer carries the
    # dynamism; the firmware ignores this flag for in-array tuples).
    v = build_abi_value(_component("f((address a, bytes data)[] xs)"))
    assert v == {
        "array": {
            "tuple": {
                "fields": [{"atomic": "ABI_ADDRESS"}, {"dynamic": "ABI_BYTES"}],
                "is_dynamic": False,
            }
        }
    }


def test_build_abi_value_static_tuple_in_array_is_unsupported():
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((uint256 a, uint256 b)[] xs)"))
    assert exc.value.feature == "static-tuple-in-array"


def test_build_abi_value_fixed_size_array_is_unsupported():
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f(uint256[2] xs)"))
    assert exc.value.feature == "fixed-size-array"


def test_build_abi_value_atomic_two_deep_array_is_kept():
    # The firmware models up to two array layers over an atomic/dynamic leaf.
    assert build_abi_value(_component("f(uint256[][] x)")) == {
        "array": {"array": {"atomic": "ABI_UINT256"}}
    }


def test_build_abi_value_three_deep_array_is_unsupported():
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f(uint256[][][] x)"))
    assert exc.value.feature == "array-nesting-too-deep"


def test_build_abi_value_tuple_in_nested_array_is_unsupported():
    # A (dynamic) tuple is fine in one array layer but not two.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((address a, bytes d)[][] xs)"))
    assert exc.value.feature == "tuple-in-nested-array"


def test_build_abi_value_nested_tuple_is_unsupported():
    # The firmware decodes tuple fields as atomic/dynamic leaves only, so a
    # tuple field that is itself a tuple or an array is rejected.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((address a, (uint256 b) inner) t)"))
    assert exc.value.feature == "non-leaf-tuple-field"


def test_build_abi_value_array_valued_tuple_field_is_unsupported():
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((uint256[] amounts, address to) t)"))
    assert exc.value.feature == "non-leaf-tuple-field"

    # also rejected when the tuple itself sits in an array
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((address a, bytes[] ds)[] xs)"))
    assert exc.value.feature == "non-leaf-tuple-field"
