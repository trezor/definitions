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


# Expected data paths, as `steps` (types.PathStep).
def F(i: int) -> dict[str, Any]:
    return {"field": i}


def IX(i: int) -> dict[str, Any]:
    return {"index": i}


EACH: dict[str, Any] = {"each": True}


def SL(start: int | None = None, end: int | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if start is not None:
        out["slice_start"] = start
    if end is not None:
        out["slice_end"] = end
    return out


def steps(*s: dict[str, Any]) -> dict[str, Any]:
    return {"steps": list(s)}


def _proto_path(p: Any) -> dict[str, Any]:
    """A serialized EthereumERC7730Path's steps, in the `steps(...)` shape."""
    return steps(
        *(
            {
                f.name: getattr(s, f.name)
                for f in s.FIELDS.values()
                if getattr(s, f.name) is not None
            }
            for s in p.steps
        )
    )


def _descriptor(
    formats: dict[str, Any],
    definitions: dict[str, Any] | None = None,
    constants: dict[str, Any] | None = None,
    deployments: list[dict[str, Any]] | None = None,
    enums: dict[str, Any] | None = None,
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
        "metadata": {"constants": constants or {}, "enums": enums or {}},
    }


# =====================================================================
#                       path_to_dict — leaf kinds
# =====================================================================


def test_path_to_dict_chain_id_container():
    # Not parsed by the `erc7730` library yet; mapped verbatim.
    assert path_to_dict("@.chainId", _inputs("f(address recipient)")) == (
        {"container_path": "CHAIN_ID"},
        KIND_NUMERIC,
    )


def test_path_to_dict_address():
    assert path_to_dict("recipient", _inputs("f(address recipient)")) == (
        steps(F(0)),
        KIND_ADDRESS,
    )


def test_path_to_dict_numeric():
    assert path_to_dict("amount", _inputs("f(uint256 amount)")) == (
        steps(F(0)),
        KIND_NUMERIC,
    )


def test_path_to_dict_bytes_and_string_and_bool_are_kind_bytes():
    # Scalar bytes/string/bool leaves are renderable only by `raw`.
    assert path_to_dict("data", _inputs("f(bytes data)")) == (steps(F(0)), KIND_BYTES)
    assert path_to_dict("s", _inputs("f(string s)")) == (steps(F(0)), KIND_BYTES)
    assert path_to_dict("ok", _inputs("f(bool ok)")) == (steps(F(0)), KIND_BYTES)
    assert path_to_dict("h", _inputs("f(bytes32 h)")) == (steps(F(0)), KIND_BYTES)


def test_path_to_dict_into_tuple():
    inputs = _inputs("swap((address srcToken, uint256 amount) desc)")
    assert path_to_dict("desc.srcToken", inputs) == (steps(F(0), F(0)), KIND_ADDRESS)
    assert path_to_dict("desc.amount", inputs) == (steps(F(0), F(1)), KIND_NUMERIC)


def test_path_to_dict_array_element_peels_one_dimension():
    # Indexing a uint256[] yields a uint256 scalar.
    assert path_to_dict("pools.[-1]", _inputs("f(uint256[] pools)")) == (
        steps(F(0), IX(-1)),
        KIND_NUMERIC,
    )
    # Indexing an address[] yields an address scalar.
    assert path_to_dict("xs.[0]", _inputs("f(address[] xs)")) == (
        steps(F(0), IX(0)),
        KIND_ADDRESS,
    )


def test_path_to_dict_unindexed_array_is_other():
    # A whole, un-indexed array is not a scalar — no formatter can render it.
    assert path_to_dict("xs", _inputs("f(address[] xs)")) == (
        steps(F(0)),
        KIND_OTHER,
    )


def test_path_to_dict_array_iteration_peels_to_element_kind():
    # A trailing `.[]` ends the path in `each`; the returned kind is the
    # per-element type, so a scalar formatter accepts it.
    assert path_to_dict("amounts.[]", _inputs("f(uint256[] amounts)")) == (
        steps(F(0), EACH),
        KIND_NUMERIC,
    )
    assert path_to_dict("xs.[]", _inputs("f(address[] xs)")) == (
        steps(F(0), EACH),
        KIND_ADDRESS,
    )
    assert path_to_dict("ds.[]", _inputs("f(bytes[] ds)")) == (
        steps(F(0), EACH),
        KIND_BYTES,
    )


def test_path_to_dict_array_iteration_on_non_array_raises():
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict("x.[]", _inputs("f(uint256 x)"))
    assert exc.value.feature == "iteration-over-non-array"


def test_path_to_dict_array_iteration_leaving_nested_array_is_other():
    # One `.[]` over a 2D array still leaves an array element — not flat-iterable.
    assert path_to_dict("x.[]", _inputs("f(uint256[][] x)")) == (
        steps(F(0), EACH),
        KIND_OTHER,
    )


@pytest.mark.parametrize(
    "path, sig",
    [
        ("x.[].[]", "f(uint256[][] x)"),
        ("xs.[].ys.[]", "f((uint256[] ys)[] xs)"),
    ],
)
def test_path_to_dict_second_iteration_raises(path, sig):
    # At most one `.[]` per path: two would display a list of lists.
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict(path, _inputs(sig))
    assert exc.value.feature == "nested-each-path"


def test_path_to_dict_one_iteration_in_a_2d_array():
    # One `.[]` plus an index over `uint256[][]` reaches a scalar.
    inputs = _inputs("f(uint256[][] x)")
    assert path_to_dict("x.[0].[]", inputs) == (
        steps(F(0), IX(0), EACH),
        KIND_NUMERIC,
    )
    assert path_to_dict("x.[].[-1]", inputs) == (
        steps(F(0), EACH, IX(-1)),
        KIND_NUMERIC,
    )


def test_path_to_dict_per_element_member():
    # The rest of the path after `.[]` walks into each element; the kind is
    # the per-element member's.
    inputs = _inputs("f((address t, bytes d)[] swaps)")
    assert path_to_dict("swaps.[].t", inputs) == (
        steps(F(0), EACH, F(0)),
        KIND_ADDRESS,
    )
    assert path_to_dict("swaps.[].d", inputs) == (
        steps(F(0), EACH, F(1)),
        KIND_BYTES,
    )


@pytest.mark.parametrize(
    "path, sig",
    [
        ("amounts.[].x", "f(uint256[] amounts, uint256 x)"),
        ("a.x", "f(uint256 a, uint256 x)"),
        ("swaps.t", "f((address t)[] swaps)"),
    ],
)
def test_path_to_dict_member_of_non_tuple_raises(path, sig):
    # Only a tuple has members, even when an outer one has a member by that name.
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict(path, _inputs(sig))
    assert exc.value.feature == "unknown-path-segment"


def test_path_to_dict_container_paths():
    assert path_to_dict("@.from", []) == ({"container_path": "FROM"}, KIND_ADDRESS)
    assert path_to_dict("@.to", []) == ({"container_path": "TO"}, KIND_ADDRESS)
    assert path_to_dict("@.value", []) == ({"container_path": "VALUE"}, KIND_NUMERIC)


def test_path_to_dict_unsupported_paths_raise_distinct_features():
    # Every drop reason is a distinct feature tag, so the log says exactly why.
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict("$.metadata.constants.x", [])
    assert exc.value.feature == "descriptor-path"
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict("nope", _inputs("f(uint256 amount)"))
    assert exc.value.feature == "unknown-path-segment"


# =====================================================================
#                       path_to_dict — byte slices
# =====================================================================


def test_path_to_dict_20_byte_slice_is_address_kind():
    # The packed-address pattern: a uint256 whose low 20 bytes are an address
    # (1inch `Address` type). The slice is the path's last step.
    assert path_to_dict("token.[-20:]", _inputs("f(uint256 token)")) == (
        steps(F(0), SL(-20)),
        KIND_ADDRESS,
    )
    assert path_to_dict("data.[0:20]", _inputs("f(bytes data)")) == (
        steps(F(0), SL(0, 20)),
        KIND_ADDRESS,
    )


def test_path_to_dict_other_slices_are_bytes_kind():
    assert path_to_dict("goodUntil.[-4:]", _inputs("f(uint256 goodUntil)")) == (
        steps(F(0), SL(-4)),
        KIND_BYTES,
    )
    assert path_to_dict("takerTraits.[:1]", _inputs("f(uint256 takerTraits)")) == (
        steps(F(0), SL(end=1)),
        KIND_BYTES,
    )


def test_path_to_dict_slice_of_array_element_is_supported():
    # Indexing peels the array dimension first; the element word then slices.
    assert path_to_dict("pools.[-1].[-20:]", _inputs("f(uint256[] pools)")) == (
        steps(F(0), IX(-1), SL(-20)),
        KIND_ADDRESS,
    )


def test_path_to_dict_slice_of_whole_array_raises():
    # Slicing an array selects *elements*, not bytes — not representable.
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict("xs.[1:3]", _inputs("f(uint256[] xs)"))
    assert exc.value.feature == "array-slice-path"


def test_path_to_dict_non_trailing_slice_raises():
    with pytest.raises(UnsupportedFeature) as exc:
        path_to_dict("x.[-20:].[0:1]", _inputs("f(uint256 x)"))
    assert exc.value.feature == "non-trailing-slice"


def test_path_to_dict_slice_after_iteration_slices_each_element():
    assert path_to_dict("xs.[].[-20:]", _inputs("f(uint256[] xs)")) == (
        steps(F(0), EACH, SL(-20)),
        KIND_ADDRESS,
    )


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
                "fields": [{"$ref": "$.display.definitions.missing", "path": "amount"}]
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


_ADDR_FIELD = {
    "f(address x)": {"fields": [{"path": "x", "label": "L", "format": "addressName"}]}
}

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
        formats={
            "f(address x)": {
                "fields": [{"path": "x", "label": "L", "format": "addressName"}]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"


def _addressname_desc(params):
    field = {"path": "x", "label": "L", "format": "addressName"}
    if params is not None:
        field["params"] = params
    return _descriptor(formats={"f(address x)": {"fields": [field]}})


@pytest.mark.parametrize(
    "types",
    [
        ["contract"],
        ["token"],
        ["wallet", "eoa", "contract"],
        ["eoa", "token"],
    ],
)
def test_addressname_contract_or_token_types_set_lookup_address(types):
    [rec] = build_display_formats(
        _addressname_desc({"types": types, "sources": ["local", "ens"]})
    )
    assert rec["field_definitions"][0]["lookup_address"] is True


@pytest.mark.parametrize(
    "params",
    [None, {}, {"types": []}, {"types": ["eoa"]}, {"types": ["wallet", "collection"]}],
)
def test_addressname_other_types_omit_lookup_address(params):
    [rec] = build_display_formats(_addressname_desc(params))
    assert "lookup_address" not in rec["field_definitions"][0]


def test_addressname_invalid_types_skips_file():
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _addressname_desc({"types": "contract"}), unsupported=unsupported
        )
    assert {feat for _src, feat, _det in unsupported} == {"invalid-address-types"}


def test_lookup_address_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "To",
            "formatter": "FORMATTER_ADDRESS_NAME",
            "lookup_address": True,
        }
    )
    assert info.lookup_address is True


# --- tokenTicker: rendered as addressName + lookup_address (adjustment) ---


def test_token_ticker_becomes_address_name_with_lookup():
    desc = _single_field_desc(
        "f(address token)", {"path": "token", "label": "Token", "format": "tokenTicker"}
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["field_definitions"] == [
        {
            "path": steps(F(0)),
            "label": "Token",
            "formatter": "FORMATTER_ADDRESS_NAME",
            "lookup_address": True,
        }
    ]
    assert [kind for _src, kind, _det in adjustments] == [
        "token-ticker-as-address-name"
    ]


def test_token_ticker_on_uint_retypes_leaf_like_address_name():
    desc = _single_field_desc(
        "f(uint256 token)", {"path": "token", "label": "Token", "format": "tokenTicker"}
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["parameter_definitions"] == [{"atomic": "ABI_ADDRESS"}]
    assert rec["field_definitions"][0]["lookup_address"] is True
    assert [kind for _src, kind, _det in adjustments] == [
        "address-in-numeric",
        "token-ticker-as-address-name",
    ]


def test_token_ticker_over_array_iteration():
    desc = _single_field_desc(
        "f(address[] tokens)",
        {"path": "tokens.[]", "label": "Tokens", "format": "tokenTicker"},
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"
    assert field["lookup_address"] is True


@pytest.mark.parametrize(
    "params, expected",
    [
        ({"chainId": 10}, {"const_chain_id": 10}),
        ({"chainIdPath": "dstChain"}, {"chain_id_path": steps(F(1))}),
    ],
    ids=["chainId", "chainIdPath"],
)
def test_token_ticker_cross_chain_emits_chain_id(params, expected):
    # Cross-chain tickers look the name up on the token's own chain.
    desc = _single_field_desc(
        "f(address token, uint256 dstChain)",
        {"path": "token", "label": "Token", "format": "tokenTicker", "params": params},
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"
    assert field["lookup_address"] is True
    assert {k: field[k] for k in expected} == expected
    assert not {"const_chain_id", "chain_id_path"} - expected.keys() & field.keys()


def test_addressname_on_uint_retypes_leaf_to_address():
    # An address packed into a uint (1inch pools, maker order receivers): the
    # field is kept, the ABI leaf is retyped so the firmware decodes an address,
    # and the reinterpretation is logged as an adjustment.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x", "label": "L", "format": "addressName"}]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"
    assert rec["parameter_definitions"] == [{"atomic": "ABI_ADDRESS"}]
    assert [(kind) for _src, kind, _det in adjustments] == ["address-in-numeric"]


def test_addressname_on_uint_in_tuple_retypes_only_that_leaf():
    desc = _descriptor(
        formats={
            "f((uint256 receiver, uint256 amount) order)": {
                "fields": [
                    {"path": "order.receiver", "label": "To", "format": "addressName"}
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [param] = rec["parameter_definitions"]
    assert param["tuple"]["fields"] == [
        {"atomic": "ABI_ADDRESS"},
        {"atomic": "ABI_UINT256"},
    ]


def test_addressname_over_uint_array_iteration_retypes_element():
    # `xs.[]` over uint256[]: the shared element type is retyped, so every
    # element decodes as an address.
    desc = _descriptor(
        formats={
            "f(uint256[] xs)": {
                "fields": [{"path": "xs.[]", "label": "L", "format": "addressName"}]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["parameter_definitions"] == [{"array": {"atomic": "ABI_ADDRESS"}}]


def test_two_addressname_fields_on_same_uint_leaf_both_kept():
    # The second field finds the leaf already retyped — that's fine, not a skip.
    desc = _descriptor(
        formats={
            "f(uint256[] pools)": {
                "fields": [
                    {
                        "path": "pools.[0]",
                        "label": "First pool",
                        "format": "addressName",
                    },
                    {
                        "path": "pools.[-1]",
                        "label": "Last pool",
                        "format": "addressName",
                    },
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert len(rec["field_definitions"]) == 2
    assert rec["parameter_definitions"] == [{"array": {"atomic": "ABI_ADDRESS"}}]


def test_addressname_on_bytes_is_kept_without_retype():
    # The firmware's AddressNameFormatter renders bytes values as hex directly;
    # the declared ABI type stays (bytes32 is left-aligned, address isn't).
    desc = _descriptor(
        formats={
            "f(bytes32 h)": {
                "fields": [{"path": "h", "label": "L", "format": "addressName"}]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["parameter_definitions"] == [{"atomic": "ABI_BYTES32"}]
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_ADDRESS_NAME"
    assert [kind for _src, kind, _det in adjustments] == ["addressname-on-bytes"]


def test_addressname_on_tuple_still_skips_file():
    # KIND_OTHER (a whole tuple/array) has no address reinterpretation.
    desc = _descriptor(
        formats={
            "f(uint256[] xs)": {
                "fields": [{"path": "xs", "label": "L", "format": "addressName"}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def _assert_unknown_token(field):
    """A tokenAmount emitted with no token reference: firmware's unknown token."""
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    assert "token_path" not in field
    assert "const_token_address" not in field


def test_tokenamount_no_token_is_unknown_token():
    # tokenAmount on a numeric value with no token reference at all: the token is
    # unknown. Emitted with no token reference.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x", "label": "L", "format": "tokenAmount"}]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    _assert_unknown_token(field)
    assert "native_currency_address" not in field
    assert [kind for _src, kind, _det in adjustments] == ["tokenamount-unknown-token"]


@pytest.mark.parametrize(
    "params, feature",
    [
        ({"tokenPath": ""}, "unresolvable-token-path"),
        ({"token": ""}, "invalid-const-token"),
        ({"token": 0}, "invalid-const-token"),
    ],
)
def test_tokenamount_empty_token_reference_skips_file(params, feature):
    # A present-but-empty tokenPath/token is a descriptor error, not "no token":
    # it must not slip through as an unknown token.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "L",
                        "format": "tokenAmount",
                        "params": params,
                    }
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {feature}


def test_tokenamount_on_bytes_skips_file():
    desc = _descriptor(
        formats={
            "f(bytes x)": {
                "fields": [{"path": "x", "label": "L", "format": "tokenAmount"}]
            }
        }
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
    assert field["token_path"] == steps(F(1))


def test_tokenpath_on_uint_retypes_leaf_to_address():
    # A token address packed into a uint (1inch `order.takerAsset`): the leaf
    # is retyped so the firmware's token_path walk yields address bytes.
    desc = _descriptor(
        formats={
            "f(uint256 amount, uint256 packedToken)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "packedToken"},
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["token_path"] == steps(F(1))
    assert rec["parameter_definitions"] == [
        {"atomic": "ABI_UINT256"},
        {"atomic": "ABI_ADDRESS"},
    ]
    assert [kind for _src, kind, _det in adjustments] == ["token-address-in-numeric"]


def test_tokenpath_on_bytes_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 amount, bytes32 notToken)": {
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
        build_display_formats(
            _const_token_desc("not-an-address"), unsupported=unsupported
        )
    assert {feat for _src, feat, _det in unsupported} == {"invalid-const-token"}


def test_tokenamount_unresolvable_constant_token_skips_file():
    # A $.metadata.constants.* reference with no matching constant is invalid.
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _const_token_desc("$.metadata.constants.missing"), unsupported=unsupported
        )
    assert {feat for _src, feat, _det in unsupported} == {"invalid-const-token"}


def test_tokenamount_literal_zero_token_with_native_is_native_currency_address():
    # A literal zero token declared native: stays FORMATTER_TOKEN_AMOUNT with
    # const_token_address zero — native_currency_address tells the firmware
    # to render it as a native amount at runtime.
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
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    assert field["const_token_address"] == "0" * 40
    assert field["native_currency_address"] == ["0" * 40]


def test_tokenamount_literal_zero_token_without_native_is_kept_and_logged():
    # A literal zero token NOT declared native: the firmware finds no such
    # token and shows an unknown token — faithful, so emitted, but flagged
    # since it's likely a descriptor slip.
    adjustments: list = []
    [rec] = build_display_formats(
        _const_token_desc("0x" + "00" * 20), adjustments=adjustments
    )
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    assert field["const_token_address"] == "0" * 40
    assert "native_currency_address" not in field
    assert [kind for _src, kind, _det in adjustments] == ["null-token-not-native"]


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
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "Amt",
            "formatter": "FORMATTER_TOKEN_AMOUNT",
            "const_token_address": "ab" * 20,
        }
    )
    assert info.const_token_address == bytes.fromhex("ab" * 20)


def test_native_currency_address_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "Amt",
            "formatter": "FORMATTER_TOKEN_AMOUNT",
            "const_token_address": "0" * 40,
            "native_currency_address": ["0" * 40, "ee" * 20],
        }
    )
    assert info.native_currency_address == [
        bytes.fromhex("0" * 40),
        bytes.fromhex("ee" * 20),
    ]


def test_threshold_message_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "Amt",
            "formatter": "FORMATTER_TOKEN_AMOUNT",
            "threshold": "ff",
            "threshold_message": "Unlimited",
        }
    )
    assert info.threshold_message == "Unlimited"


def test_const_value_path_serializes_to_proto():
    from ..serialize import _build_erc7730_path

    info = _build_erc7730_path({"const_value": "kmgcEURC"})
    assert info.const_value == "kmgcEURC"
    assert info.path == []
    assert info.container_path is None


@pytest.mark.parametrize(
    "path",
    [
        steps(F(0), IX(-1)),  # `pools.[-1]`: index is sint32, negatives round-trip
        steps(F(1), EACH),  # `amounts.[]`
        steps(F(0), IX(-1), SL(-20)),  # `pools.[-1].[-20:]`
        steps(F(0), SL(0, 4)),  # `data.[0:4]`
    ],
    ids=["negative-index", "each", "index-then-slice", "two-bound-slice"],
)
def test_steps_path_serializes_to_proto(path):
    import io

    from trezorlib import protobuf

    from ..serialize import _build_erc7730_path

    info = _build_erc7730_path(path)
    buf = io.BytesIO()
    protobuf.dump_message(buf, info)
    buf.seek(0)
    decoded = protobuf.load_message(buf, type(info))
    assert _proto_path(decoded) == path
    # Steps only: the deprecated flat form is never set.
    assert decoded.path == []
    assert decoded.slice_start is None and decoded.slice_end is None


def test_path_step_with_two_kinds_is_rejected():
    from ..serialize import _build_erc7730_path

    with pytest.raises(ValueError):
        _build_erc7730_path({"steps": [{"field": 0, "index": 1}]})


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


def test_tokenamount_no_token_with_null_native_is_unknown_token():
    # No tokenPath/token: there's no token address for nativeCurrencyAddress
    # to match, so its zero entry doesn't apply and the token is unknown.
    desc = _tokenamount_native_desc(["0x" + "ee" * 20, "0x" + "00" * 20])
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    _assert_unknown_token(field)
    # Carried along but inert: there's no token address for it to match.
    assert field["native_currency_address"] == ["ee" * 20, "0" * 40]


def test_tokenamount_no_token_via_constant_null_is_unknown_token():
    # Same with the zero-address sentinel as a $.metadata.constants.* reference.
    desc = _tokenamount_native_desc(
        ["$.metadata.constants.addressAsNull"],
        constants={"addressAsNull": "0x" + "00" * 20},
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    _assert_unknown_token(field)


def test_tokenamount_shared_definition_null_native_needs_own_token_path():
    # 1inch AggregationRouterV6 shape: a shared `minReceiveAmount` definition
    # carries nativeCurrencyAddress [0xEeee…, 0x0]. `swap` adds a tokenPath
    # when it refs it; `unoswap` refs it bare, because its output token is not
    # in calldata. For `swap` the list applies to whatever `dstToken` holds
    # (0x0 → native). For `unoswap` there's no token address for it to match,
    # so it must come out as an unknown token, not as native.
    native = ["$.metadata.constants.addressAsEth", "$.metadata.constants.addressAsNull"]
    desc = _descriptor(
        definitions={
            "minReceiveAmount": {
                "label": "Minimum to Receive",
                "format": "tokenAmount",
                "params": {"nativeCurrencyAddress": native},
            }
        },
        constants={
            "addressAsEth": "0x" + "ee" * 20,
            "addressAsNull": "0x" + "00" * 20,
        },
        formats={
            "swap(address dstToken, uint256 minReturn)": {
                "fields": [
                    {
                        "path": "minReturn",
                        "$ref": "$.display.definitions.minReceiveAmount",
                        "params": {"tokenPath": "dstToken"},
                        "visible": "always",
                    }
                ]
            },
            "unoswap(uint256 minReturn, uint256 dex)": {
                "fields": [
                    {
                        "path": "minReturn",
                        "$ref": "$.display.definitions.minReceiveAmount",
                        "visible": "always",
                    }
                ]
            },
        },
    )
    swap, unoswap = build_display_formats(desc)
    assert swap["func_sig"] == "0xd004f0f7"  # swap(address,uint256)
    [field] = swap["field_definitions"]
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    assert field["token_path"] == steps(F(0))
    assert field["native_currency_address"] == ["ee" * 20, "0" * 40]
    assert "const_token_address" not in field
    [field] = unoswap["field_definitions"]
    _assert_unknown_token(field)


def test_tokenamount_no_token_without_null_native_is_unknown_token():
    desc = _tokenamount_native_desc(["0x" + "ee" * 20])
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    _assert_unknown_token(field)


def test_tokenamount_no_token_keeps_threshold():
    # `threshold` compares the raw amount, so it needs no token to apply.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "L",
                        "format": "tokenAmount",
                        "params": {"threshold": "0xff", "message": "Unlimited"},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    _assert_unknown_token(field)
    assert field["threshold"] == "ff"
    assert field["threshold_message"] == "Unlimited"


def test_tokenamount_invalid_native_currency_address_skips_file():
    # An unresolvable nativeCurrencyAddress entry is a descriptor error, not
    # silently ignorable -> skip the file.
    desc = _tokenamount_native_desc(["not-an-address"])
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {
        "invalid-native-currency-address"
    }


def test_container_value_token_amount_without_token_is_unknown_token():
    # @.value is numeric (kind-compatible with tokenAmount). With no token
    # reference there's no address for the zero native sentinel to match, so
    # it's an unknown token on the VALUE path too. A native value is the
    # `amount` format.
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
    _assert_unknown_token(field)


def test_container_value_rejects_address_name():
    desc = _descriptor(
        formats={
            "f()": {
                "fields": [{"path": "@.value", "label": "X", "format": "addressName"}]
            }
        }
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


# --- label / decimals sanity (#8) ---


def test_empty_label_on_displayed_field_skips_file():
    desc = _descriptor(
        formats={
            "f(address x)": {
                "fields": [{"path": "x", "label": "", "format": "addressName"}]
            }
        }
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


def _threshold_message_desc(threshold, message):
    params: dict = {"tokenPath": "token"}
    if threshold is not None:
        params["threshold"] = threshold
    if message is not None:
        params["message"] = message
    return _descriptor(
        formats={
            "f(uint256 amount, address token)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": params,
                    }
                ]
            }
        }
    )


def test_tokenamount_message_is_kept_alongside_threshold():
    [rec] = build_display_formats(_threshold_message_desc("0xff", "Unlimited"))
    assert rec["field_definitions"][0]["threshold_message"] == "Unlimited"


def test_tokenamount_message_without_threshold_is_dropped():
    [rec] = build_display_formats(_threshold_message_desc(None, "Unlimited"))
    assert "threshold_message" not in rec["field_definitions"][0]


def test_tokenamount_threshold_without_message_omits_key():
    [rec] = build_display_formats(_threshold_message_desc("0xff", None))
    assert "threshold_message" not in rec["field_definitions"][0]


def test_unit_valid_decimals_is_kept():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "Gas",
                        "format": "unit",
                        "params": {"decimals": 9},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["decimals"] == 9


def test_unit_negative_decimals_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "Gas",
                        "format": "unit",
                        "params": {"decimals": -1},
                    }
                ]
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
                "fields": [
                    {
                        "path": "x",
                        "label": "Gas",
                        "format": "unit",
                        "params": {"decimals": "nine"},
                    }
                ]
            }
        }
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


def test_unit_base_literal_is_kept():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "Stake amount",
                        "format": "unit",
                        "params": {"base": "POL", "decimals": 18},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["base"] == "POL"


def test_unit_base_constant_ref_is_resolved():
    # `base` may be a $.metadata.constants.* reference (yieldxyz staking tokens).
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "Stake amount",
                        "format": "unit",
                        "params": {
                            "base": "$.metadata.constants.stakingTokenTicker",
                            "decimals": 18,
                        },
                    }
                ]
            }
        },
        constants={"stakingTokenTicker": "POL"},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["base"] == "POL"


def test_unit_base_unresolvable_constant_ref_skips_file():
    unsupported: list = []
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "Stake amount",
                        "format": "unit",
                        "params": {
                            "base": "$.metadata.constants.missing",
                            "decimals": 18,
                        },
                    }
                ]
            }
        }
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"unresolvable-constant-value"}


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
        {
            "path": "t",
            "label": "Deadline",
            "format": "date",
            "params": {"encoding": "timestamp"},
        },
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_DATE"


def test_date_blockheight_encoding_falls_back_to_raw():
    # A block number is not a time — render it as a plain integer, not a date.
    desc = _single_field_desc(
        "f(uint256 b)",
        {
            "path": "b",
            "label": "Block",
            "format": "date",
            "params": {"encoding": "blockheight"},
        },
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


def test_duration_is_duration_formatter():
    desc = _single_field_desc(
        "f(uint256 t)", {"path": "t", "label": "Lock time", "format": "duration"}
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_DURATION"


def test_duration_on_non_numeric_skips_file():
    desc = _single_field_desc(
        "f(address x)", {"path": "x", "label": "L", "format": "duration"}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_chain_id_is_chain_id_formatter():
    desc = _single_field_desc(
        "f(uint256 c)", {"path": "c", "label": "Chain", "format": "chainId"}
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["field_definitions"] == [
        {"path": steps(F(0)), "label": "Chain", "formatter": "FORMATTER_CHAIN_ID"}
    ]
    assert adjustments == []


def test_chain_id_over_container_chain_id():
    desc = _single_field_desc(
        "f(uint256 c)", {"path": "@.chainId", "label": "Chain", "format": "chainId"}
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_CHAIN_ID"
    assert field["path"] == {"container_path": "CHAIN_ID"}


def test_chain_id_on_non_numeric_skips_file():
    desc = _single_field_desc(
        "f(address x)", {"path": "x", "label": "L", "format": "chainId"}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_chain_id_serializes_to_proto():
    from trezorlib.messages import EthereumERC7730FieldFormatterType

    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {"path": steps(F(0)), "label": "Chain", "formatter": "FORMATTER_CHAIN_ID"}
    )
    assert info.formatter == EthereumERC7730FieldFormatterType.FORMATTER_CHAIN_ID


# =====================================================================
#                array (multi-value) formatters via `.[]`
# =====================================================================


def test_amount_over_array_is_kept():
    # `amounts.[]` renders the amount formatter once per array element on-device;
    # the emitted path ends in `each`.
    desc = _single_field_desc(
        "f(uint256[] amounts)",
        {
            "path": "amounts.[]",
            "label": "Amounts",
            "format": "unit",
            "params": {"decimals": 9},
        },
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), EACH)
    assert field["formatter"] == "FORMATTER_UNIT"


def test_addressname_over_array_is_kept():
    desc = _single_field_desc(
        "f(address[] recipients)",
        {"path": "recipients.[]", "label": "To", "format": "addressName"},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_ADDRESS_NAME"


def test_raw_over_array_is_kept():
    desc = _single_field_desc(
        "f(bytes[] datas)", {"path": "datas.[]", "label": "Data", "format": "raw"}
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["formatter"] == "FORMATTER_RAW"


def test_tokenamount_over_array_with_shared_token_path():
    # An array of amounts sharing a single token: the amount path iterates, the
    # token_path resolves to one (scalar) token address.
    desc = _descriptor(
        formats={
            "f(uint256[] amounts, address token)": {
                "fields": [
                    {
                        "path": "amounts.[]",
                        "label": "Amounts",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "token"},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), EACH)
    assert field["formatter"] == "FORMATTER_TOKEN_AMOUNT"
    assert field["token_path"] == steps(F(1))


def test_tokenamount_over_array_with_per_element_token_path():
    # Amount i is shown with token i: the firmware pairs a per-element param
    # with the field's elements by index.
    desc = _single_field_desc(
        "f(uint256[] amounts, address[] tokens)",
        {
            "path": "amounts.[]",
            "label": "Amounts",
            "format": "tokenAmount",
            "params": {"tokenPath": "tokens.[]"},
        },
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), EACH)
    assert field["token_path"] == steps(F(1), EACH)


@pytest.mark.parametrize(
    "signature, field",
    [
        (
            "f(uint256 amount, address[] tokens)",
            {
                "path": "amount",
                "format": "tokenAmount",
                "params": {"tokenPath": "tokens.[]"},
            },
        ),
        (
            "f(bytes data, address[] targets)",
            {
                "path": "data",
                "format": "calldata",
                "params": {"calleePath": "targets.[]"},
            },
        ),
    ],
)
def test_per_element_param_on_scalar_field_skips_file(signature, field):
    # A single value has no "element i" to pair the param's elements with.
    desc = _single_field_desc(signature, {"label": "L", **field})
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {
        "per-element-param-without-each"
    }


def test_registry_morpho_offer_fills():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/4b7ff1c2df803f3e14f801bea9ecc779b6fa8b79/registry/morpho/calldata-MidnightBundlesV2.json#L237
    # Each fill's units in its offer's loan token. The hidden field's second
    # `.[]` doesn't drop the format: hidden fields aren't built.
    desc = _descriptor(
        formats={
            "f(((address maker, (address loanToken, (address token, uint256 lltv)[]"
            " collateralParams) market) offer, uint256 units)[] offerFills)": {
                "fields": [
                    {
                        "path": "#.offerFills.[].units",
                        "label": "Fill amount",
                        "format": "tokenAmount",
                        "params": {
                            "tokenPath": "#.offerFills.[].offer.market.loanToken"
                        },
                    },
                    {
                        "path": "#.offerFills.[].offer.market.collateralParams.[].token",
                        "label": "Collateral token",
                        "format": "raw",
                        "visible": "never",
                    },
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), EACH, F(1))
    assert field["token_path"] == steps(F(0), EACH, F(0), F(1), F(0))


def test_registry_morpho_bundle_calls():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/4b7ff1c2df803f3e14f801bea9ecc779b6fa8b79/registry/morpho/calldata-MorphoBundlerV3.json#L18
    desc = _descriptor(
        formats={
            "multicall((address to, bytes data, uint256 value, bool skipRevert,"
            " bytes32 callbackHash)[] bundle)": {
                "fields": [
                    {
                        "path": "#.bundle.[].data",
                        "label": "Action",
                        "format": "calldata",
                        "params": {
                            "calleePath": "#.bundle.[].to",
                            "amountPath": "#.bundle.[].value",
                        },
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), EACH, F(1))
    assert field["callee_path"] == steps(F(0), EACH, F(0))
    assert field["amount_path"] == steps(F(0), EACH, F(2))


def test_registry_lifi_swap_steps():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/4b7ff1c2df803f3e14f801bea9ecc779b6fa8b79/registry/lifi/calldata-LIFIDiamond.json#L3296
    # Every swap step has its own token (WETH, then USDC, ...): paired by index.
    desc = _descriptor(
        formats={
            "f((address receiver, uint256 minAmount) _bridgeData, (address callTo,"
            " address approveTo, address sendingAssetId, address receivingAssetId,"
            " uint256 fromAmount, bytes callData, bool requiresDeposit)[] _swapData)": {
                "fields": [
                    {
                        "path": "_swapData.[].fromAmount",
                        "label": "Swap Step Input",
                        "format": "tokenAmount",
                        "params": {
                            "tokenPath": "_swapData.[].sendingAssetId",
                            "nativeCurrencyAddress": "0x" + "00" * 20,
                        },
                    },
                    {
                        "path": "_swapData.[].requiresDeposit",
                        "label": "Swap Step Taken From Wallet",
                        "format": "raw",
                    },
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    amount, deposit = rec["field_definitions"]
    assert amount["path"] == steps(F(1), EACH, F(4))
    assert amount["token_path"] == steps(F(1), EACH, F(2))
    assert deposit["path"] == steps(F(1), EACH, F(6))


def test_registry_lifi_squid_source_calls():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/4b7ff1c2df803f3e14f801bea9ecc779b6fa8b79/registry/lifi/calldata-LIFIDiamond.json#L1468
    desc = _descriptor(
        formats={
            "f((uint8 routeType, (uint8 callType, address target, uint256 value,"
            " bytes callData, bytes payload)[] sourceCalls) _squidData)": {
                "fields": [
                    {
                        "path": "_squidData.sourceCalls.[].target",
                        "label": "Source Call Target",
                        "format": "addressName",
                        "params": {"types": ["eoa", "contract"]},
                    },
                    {
                        "path": "_squidData.sourceCalls.[].callData",
                        "label": "Source Call Data",
                        "format": "raw",
                    },
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    target, data = rec["field_definitions"]
    assert target["path"] == steps(F(0), F(1), EACH, F(1))
    assert target["formatter"] == "FORMATTER_ADDRESS_NAME"
    assert data["path"] == steps(F(0), F(1), EACH, F(3))


def test_unindexed_array_without_iteration_skips_file():
    # A bare array path (no `.[]`) is still unrenderable.
    desc = _single_field_desc(
        "f(uint256[] amounts)",
        {"path": "amounts", "label": "Amounts", "format": "amount"},
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
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x", "label": "L", "format": _UNSUPPORTED_FORMAT}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, source="prov/file.json", unsupported=unsupported)
    assert unsupported[0][0] == "prov/file.json"
    assert unsupported[0][1] == "unsupported-formatter"


def test_raw_constant_field_emits_const_value():
    # A displayed field bound to a literal constant (no calldata `path`) rides
    # in the proto as a const_value path, rendered as-is by the raw formatter.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"label": "Summary", "format": "raw", "value": "hi"}]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field == {
        "path": {"const_value": "hi"},
        "label": "Summary",
        "formatter": "FORMATTER_RAW",
    }
    assert [kind for _src, kind, _det in adjustments] == ["constant-value-field"]


def test_constant_field_resolves_metadata_constant():
    # The `value` may be a $.metadata.constants.* reference (kiln vault tickers).
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "label": "Share ticker",
                        "format": "raw",
                        "value": "$.metadata.constants.vaultTicker",
                    }
                ]
            }
        },
        constants={"vaultTicker": "kmgcEURC"},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["path"] == {"const_value": "kmgcEURC"}


def test_constant_field_with_unresolvable_constant_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "label": "L",
                        "format": "raw",
                        "value": "$.metadata.constants.missing",
                    }
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"unresolvable-constant-value"}


def test_constant_field_with_non_raw_formatter_skips_file():
    # Only `raw` can render a pre-materialized constant string on-device.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"label": "L", "format": "amount", "value": "5"}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"constant-value-formatter"}


def test_constant_field_stringifies_numbers_and_logs():
    desc = _descriptor(
        formats={
            "f(uint256 x)": {"fields": [{"label": "N", "format": "raw", "value": 42}]}
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["field_definitions"][0]["path"] == {"const_value": "42"}
    assert {kind for _src, kind, _det in adjustments} == {
        "constant-value-field",
        "constant-value-stringified",
    }


def test_hidden_constant_field_is_skipped_silently():
    desc = _descriptor(
        formats={
            "f(address to)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {"label": "L", "format": "raw", "value": "x", "visible": "never"},
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert len(rec["field_definitions"]) == 1


def test_const_field_end_to_end_keeps_display_format():
    # kiln-style deposit: an amount, a constant "Share ticker" label, a recipient.
    # The constant no longer drops the whole display format.
    desc = _descriptor(
        formats={
            "deposit(uint256 assets, address receiver)": {
                "fields": [
                    {
                        "path": "assets",
                        "label": "Deposit asset",
                        "format": "tokenAmount",
                        "params": {"token": "0x" + "ab" * 20},
                    },
                    {
                        "label": "Share ticker",
                        "format": "raw",
                        "value": "$.metadata.constants.vaultTicker",
                    },
                    {
                        "path": "receiver",
                        "label": "Send shares to",
                        "format": "addressName",
                    },
                ]
            }
        },
        constants={"vaultTicker": "kmgcEURC"},
    )
    [rec] = build_display_formats(desc)
    fields = rec["field_definitions"]
    assert len(fields) == 3
    assert fields[1] == {
        "path": {"const_value": "kmgcEURC"},
        "label": "Share ticker",
        "formatter": "FORMATTER_RAW",
    }


def test_const_value_field_info_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": {"const_value": "kmgcEURC"},
            "label": "Share ticker",
            "formatter": "FORMATTER_RAW",
        }
    )
    assert info.path.const_value == "kmgcEURC"


def test_constant_path_field_emits_const_value():
    # A field whose `path` is a $.metadata.constants.* reference is a constant
    # too, resolved at parse time.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [
                    {
                        "path": "$.metadata.constants.note",
                        "label": "Note",
                        "format": "raw",
                    }
                ]
            }
        },
        constants={"note": "hello"},
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["path"] == {"const_value": "hello"}


def test_non_path_non_value_displayed_field_skips_file():
    # Displayed, but neither a calldata path nor a constant value — nothing to show.
    desc = _descriptor(
        formats={"f(uint256 x)": {"fields": [{"label": "L", "format": "raw"}]}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"non-path-field"}


# =====================================================================
#                sliced fields end-to-end (1inch patterns)
# =====================================================================


def test_addressname_on_20_byte_slice_no_retype_no_adjustment():
    # `dex.[-20:]` + addressName: the slice makes the value address bytes on
    # device, so the field is faithful — no ABI retype, no adjustment.
    desc = _descriptor(
        formats={
            "unoswap(uint256 minReturn, uint256 dex)": {
                "fields": [
                    {
                        "path": "dex.[-20:]",
                        "label": "Last pool",
                        "format": "addressName",
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(1), SL(-20))
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"
    # The declared ABI type stays uint256 — the slice does the interpreting.
    assert rec["parameter_definitions"] == [
        {"atomic": "ABI_UINT256"},
        {"atomic": "ABI_UINT256"},
    ]
    assert adjustments == []


def test_date_on_sliced_word_is_kept():
    # `goodUntil.[-4:]`: the firmware's DateFormatter converts the sliced
    # big-endian bytes to the integer timestamp.
    desc = _descriptor(
        formats={
            "f(uint256 goodUntil)": {
                "fields": [
                    {
                        "path": "goodUntil.[-4:]",
                        "label": "Expiration time",
                        "format": "date",
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), SL(-4))
    assert field["formatter"] == "FORMATTER_DATE"


def test_date_on_unsliced_bytes_still_skips_file():
    # The bytes allowance is slice-only; date over plain bytes stays a drop.
    desc = _descriptor(
        formats={
            "f(bytes32 x)": {"fields": [{"path": "x", "label": "T", "format": "date"}]}
        }
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


def test_duration_on_sliced_word_is_kept():
    # `lockedFor.[-4:]`: the firmware's DurationFormatter converts the sliced
    # big-endian bytes to the integer number of seconds.
    desc = _descriptor(
        formats={
            "f(uint256 lockedFor)": {
                "fields": [
                    {
                        "path": "lockedFor.[-4:]",
                        "label": "Lock time",
                        "format": "duration",
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), SL(-4))
    assert field["formatter"] == "FORMATTER_DURATION"


def test_duration_on_unsliced_bytes_still_skips_file():
    # The bytes allowance is slice-only; duration over plain bytes stays a drop.
    desc = _descriptor(
        formats={
            "f(bytes32 x)": {
                "fields": [{"path": "x", "label": "T", "format": "duration"}]
            }
        }
    )
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc)


def test_tokenpath_with_20_byte_slice_is_kept():
    # 1inch unoswap: the token address is the low 20 bytes of a uint256.
    desc = _descriptor(
        formats={
            "unoswap(uint256 token, uint256 amount)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amount to Send",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "token.[-20:]"},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["token_path"] == steps(F(0), SL(-20))
    # No retype: the slice yields address bytes at runtime.
    assert rec["parameter_definitions"][0] == {"atomic": "ABI_UINT256"}


def test_raw_on_sliced_word_is_kept():
    # `takerTraits.[:1]`: the first byte of a packed flags word, shown as hex.
    desc = _descriptor(
        formats={
            "f(uint256 takerTraits)": {
                "fields": [
                    {
                        "path": "takerTraits.[:1]",
                        "label": "Additional action",
                        "format": "raw",
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), SL(end=1))
    assert field["formatter"] == "FORMATTER_RAW"


def test_amount_on_sliced_word_skips_file():
    # AmountFormatter needs an int; a sliced value is bytes on-device.
    desc = _descriptor(
        formats={
            "f(uint256 x)": {
                "fields": [{"path": "x.[-8:]", "label": "A", "format": "amount"}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_tokenpath_32_byte_word_slice_is_narrowed():
    # paraswap Balancer: `tokenPath: #.data.[292:324]` slices the whole
    # ABI-encoded word out of a bytes blob; the token address is the word's
    # low 20 bytes, so the slice narrows to [304:324].
    desc = _descriptor(
        formats={
            "f(uint256 amount, bytes data)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "data.[292:324]"},
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["token_path"] == steps(F(1), SL(304, 324))
    assert [k for _s, k, _d in adjustments] == ["address-in-word-slice"]


def test_addressname_on_32_byte_word_slice_is_narrowed():
    desc = _descriptor(
        formats={
            "f(uint256 w)": {
                "fields": [{"path": "w.[-32:]", "label": "To", "format": "addressName"}]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), SL(-20))
    assert field["formatter"] == "FORMATTER_ADDRESS_NAME"
    assert [k for _s, k, _d in adjustments] == ["address-in-word-slice"]


def test_calldata_callee_in_word_slice_is_narrowed():
    desc = _descriptor(
        formats={
            "f(bytes data)": {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": {"calleePath": "data.[0:32]"},
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["field_definitions"][0]["callee_path"] == steps(F(0), SL(12, 32))
    assert [k for _s, k, _d in adjustments] == ["address-in-word-slice"]


def test_tokenpath_odd_slice_length_still_drops():
    # Only exact 32-byte word slices narrow; anything else stays unresolvable.
    desc = _descriptor(
        formats={
            "f(uint256 amount, bytes data)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amt",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "data.[0:16]"},
                    }
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"unresolvable-token-path"}


# =====================================================================
#                       calldata formatter
# =====================================================================


def test_calldata_with_callee_path_is_kept():
    desc = _descriptor(
        formats={
            "execute(address target, bytes data)": {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": {"calleePath": "target"},
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_CALLDATA"
    assert field["callee_path"] == steps(F(0))
    assert "selector" not in field
    assert adjustments == []


def test_calldata_selector_is_normalized():
    # `selector` marks args-only embedded calldata; 4 bytes, hex-normalized.
    desc = _descriptor(
        formats={
            "execute(address target, bytes data)": {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": {"calleePath": "target", "selector": "0x12345678"},
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["selector"] == "12345678"


def test_calldata_bad_selector_skips_file():
    desc = _descriptor(
        formats={
            "execute(address target, bytes data)": {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": {"calleePath": "target", "selector": "0x1234"},
                    }
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"invalid-calldata-selector"}


def test_calldata_without_callee_skips_file():
    # The firmware requires callee_path to resolve the nested display format.
    desc = _descriptor(
        formats={
            "f(bytes data)": {
                "fields": [{"path": "data", "label": "Call", "format": "calldata"}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"calldata-missing-callee"}


def test_calldata_unknown_params_ignored_and_logged():
    # Params outside the spec have no proto representation; the nested call
    # still renders fully via the callee's display format.
    desc = _descriptor(
        formats={
            "execute(address target, bytes data)": {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": {"calleePath": "target", "bogus": 1},
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert [kind for _src, kind, _det in adjustments] == ["calldata-params-ignored"]
    assert "bogus" in adjustments[0][2]


def test_calldata_callee_in_packed_uint_is_retyped():
    desc = _descriptor(
        formats={
            "execute(uint256 target, bytes data)": {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": {"calleePath": "target"},
                    }
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["field_definitions"][0]["callee_path"] == steps(F(0))
    assert rec["parameter_definitions"][0] == {"atomic": "ABI_ADDRESS"}
    assert [kind for _src, kind, _det in adjustments] == ["callee-address-in-numeric"]


def test_calldata_on_non_bytes_skips_file():
    desc = _descriptor(
        formats={
            "f(uint256 x, address t)": {
                "fields": [
                    {
                        "path": "x",
                        "label": "L",
                        "format": "calldata",
                        "params": {"calleePath": "t"},
                    }
                ]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_calldata_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(1)),
            "label": "Call",
            "formatter": "FORMATTER_CALLDATA",
            "callee_path": steps(F(0)),
            "selector": "12345678",
        }
    )
    assert _proto_path(info.callee_path) == steps(F(0))
    assert info.selector == bytes.fromhex("12345678")


_CALLDATA_SIG = (
    "execute(address target, bytes data, uint256 amount, address safe, "
    "uint256 packed, string name, bytes4 sel, bytes32 word)"
)


def _calldata_desc(params, constants=None):
    return _descriptor(
        formats={
            _CALLDATA_SIG: {
                "fields": [
                    {
                        "path": "data",
                        "label": "Call",
                        "format": "calldata",
                        "params": params,
                    }
                ]
            }
        },
        constants=constants,
    )


@pytest.mark.parametrize(
    "params, constants, expected",
    [
        (
            {"callee": "0x" + "AB" * 20},
            None,
            {"const_callee": "ab" * 20},
        ),
        (
            {"callee": "$.metadata.constants.router"},
            {"router": "0x" + "cd" * 20},
            {"const_callee": "cd" * 20},
        ),
        (
            {"calleePath": "target", "amountPath": "amount"},
            None,
            {"callee_path": steps(F(0)), "amount_path": steps(F(2))},
        ),
        (
            {"calleePath": "target", "amountPath": "@.value"},
            None,
            {"callee_path": steps(F(0)), "amount_path": {"container_path": "VALUE"}},
        ),
        (
            {"calleePath": "target", "spenderPath": "safe"},
            None,
            {"callee_path": steps(F(0)), "spender_path": steps(F(3))},
        ),
        (
            {"calleePath": "target", "spenderPath": "@.from"},
            None,
            {"callee_path": steps(F(0)), "spender_path": {"container_path": "FROM"}},
        ),
        (
            {"calleePath": "target", "spender": "0x" + "ef" * 20},
            None,
            {"callee_path": steps(F(0)), "const_spender": "ef" * 20},
        ),
        (
            {"calleePath": "target", "amount": 10**18},
            None,
            {"callee_path": steps(F(0)), "const_amount": "0de0b6b3a7640000"},
        ),
        (
            {"calleePath": "target", "amount": 0},
            None,
            {"callee_path": steps(F(0)), "const_amount": "00"},
        ),
        (
            {"calleePath": "target", "amount": 2**256 - 1},
            None,
            {"callee_path": steps(F(0)), "const_amount": "ff" * 32},
        ),
        (
            {"calleePath": "target", "amount": "$.metadata.constants.fee"},
            {"fee": "0x100"},
            {"callee_path": steps(F(0)), "const_amount": "0100"},
        ),
        (
            {"calleePath": "target", "selectorPath": "sel"},
            None,
            {"callee_path": steps(F(0)), "selector_path": steps(F(6))},
        ),
        (
            {"calleePath": "target", "selectorPath": "word.[0:4]"},
            None,
            {
                "callee_path": steps(F(0)),
                "selector_path": steps(F(7), SL(0, 4)),
            },
        ),
        (
            {"calleePath": "target", "selectorPath": "data.[:4]"},
            None,
            {
                "callee_path": steps(F(0)),
                "selector_path": steps(F(1), SL(end=4)),
            },
        ),
    ],
    ids=[
        "const-callee",
        "const-callee-ref",
        "amountPath",
        "amountPath-container",
        "spenderPath",
        "spenderPath-container",
        "const-spender",
        "const-amount",
        "const-amount-zero",
        "const-amount-uint256-max",
        "const-amount-ref",
        "selectorPath-bytes4",
        "selectorPath-word-slice",
        "selectorPath-blob-slice",
    ],
)
def test_calldata_params_emitted(params, constants, expected):
    adjustments: list = []
    [rec] = build_display_formats(
        _calldata_desc(params, constants), adjustments=adjustments
    )
    [field] = rec["field_definitions"]
    emitted = {
        k: field[k]
        for k in (
            "callee_path",
            "const_callee",
            "selector",
            "selector_path",
            "amount_path",
            "const_amount",
            "spender_path",
            "const_spender",
        )
        if k in field
    }
    assert emitted == expected
    assert adjustments == []


def test_calldata_spender_in_packed_uint_is_retyped():
    adjustments: list = []
    [rec] = build_display_formats(
        _calldata_desc({"calleePath": "target", "spenderPath": "packed"}),
        adjustments=adjustments,
    )
    assert rec["field_definitions"][0]["spender_path"] == steps(F(4))
    assert rec["parameter_definitions"][4] == {"atomic": "ABI_ADDRESS"}
    assert [kind for _src, kind, _det in adjustments] == ["spender-address-in-numeric"]


@pytest.mark.parametrize(
    "params, feature",
    [
        ({"callee": "0x" + "ab" * 20, "calleePath": "target"}, "conflicting-params"),
        (
            {"calleePath": "target", "amount": 1, "amountPath": "amount"},
            "conflicting-params",
        ),
        (
            {
                "calleePath": "target",
                "spender": "0x" + "ab" * 20,
                "spenderPath": "safe",
            },
            "conflicting-params",
        ),
        (
            {"calleePath": "target", "selector": "0x12345678", "selectorPath": "name"},
            "conflicting-params",
        ),
        ({"callee": "0x1234"}, "invalid-const-callee"),
        (
            {"callee": {"$ref": "$.metadata.maps.x", "keyPath": "@.chainId"}},
            "invalid-const-callee",
        ),
        (
            {"calleePath": "target", "selectorPath": "name"},
            "unresolvable-selector-path",
        ),
        (
            {"calleePath": "target", "selectorPath": "word"},
            "unresolvable-selector-path",
        ),
        (
            {"calleePath": "target", "selectorPath": "word.[0:8]"},
            "unresolvable-selector-path",
        ),
        (
            {"calleePath": "target", "selectorPath": "nope"},
            "unresolvable-selector-path",
        ),
        ({"calleePath": "target", "amount": -1}, "invalid-amount"),
        ({"calleePath": "target", "amount": 2**256}, "invalid-amount"),
        ({"calleePath": "target", "amount": True}, "invalid-amount"),
        (
            {"calleePath": "target", "amount": "$.metadata.constants.missing"},
            "unresolvable-amount",
        ),
        ({"calleePath": "target", "amountPath": "safe"}, "unresolvable-amount-path"),
        ({"calleePath": "target", "amountPath": "nope"}, "unresolvable-amount-path"),
        ({"calleePath": "target", "spenderPath": "name"}, "unresolvable-spender-path"),
        ({"calleePath": "target", "spender": "nope"}, "invalid-const-spender"),
    ],
    ids=[
        "callee-both",
        "amount-both",
        "spender-both",
        "selector-both",
        "bad-const-callee",
        "callee-map-ref",
        "selectorPath-string",
        "selectorPath-bytes32",
        "selectorPath-8-byte-slice",
        "selectorPath-unknown",
        "amount-negative",
        "amount-over-uint256",
        "amount-bool",
        "amount-missing-constant",
        "amountPath-address",
        "amountPath-unknown",
        "spenderPath-string",
        "bad-const-spender",
    ],
)
def test_calldata_bad_params_skip_file(params, feature):
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(_calldata_desc(params), unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {feature}


def test_calldata_new_params_serialize_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(1)),
            "label": "Call",
            "formatter": "FORMATTER_CALLDATA",
            "const_callee": "ab" * 20,
            "selector_path": steps(F(6)),
            "const_amount": "0de0b6b3a7640000",
            "amount_path": {"container_path": "VALUE"},
            "spender_path": steps(F(3)),
            "const_spender": "cd" * 20,
        }
    )
    assert info.callee_path is None
    assert info.const_callee == bytes.fromhex("ab" * 20)
    assert _proto_path(info.selector_path) == steps(F(6))
    assert info.selector is None
    assert int.from_bytes(info.const_amount, "big") == 10**18
    assert info.amount_path.container_path == 2  # VALUE
    assert _proto_path(info.spender_path) == steps(F(3))
    assert info.const_spender == bytes.fromhex("cd" * 20)


# =====================================================================
#                       token chain id (tokenAmount)
# =====================================================================


def _token_chain_desc(params, constants=None):
    return _descriptor(
        formats={
            "swap(address token, uint256 amount, uint64 dstChain, address to)": {
                "fields": [
                    {
                        "path": "amount",
                        "label": "Amount",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "token", **params},
                    }
                ]
            }
        },
        constants=constants,
    )


@pytest.mark.parametrize(
    "params, constants, expected",
    [
        ({"chainId": 42161}, None, {"const_chain_id": 42161}),
        ({"chainId": "0xa"}, None, {"const_chain_id": 10}),
        ({"chainId": 2**64 - 1}, None, {"const_chain_id": 2**64 - 1}),
        (
            {"chainId": "$.metadata.constants.dst"},
            {"dst": 8453},
            {"const_chain_id": 8453},
        ),
        ({"chainIdPath": "dstChain"}, None, {"chain_id_path": steps(F(2))}),
        (
            {"chainIdPath": "@.chainId"},
            None,
            {"chain_id_path": {"container_path": "CHAIN_ID"}},
        ),
    ],
    ids=["int", "hex-str", "uint64-max", "constant-ref", "path", "container"],
)
def test_token_amount_chain_id_emitted(params, constants, expected):
    [rec] = build_display_formats(_token_chain_desc(params, constants))
    [field] = rec["field_definitions"]
    assert field["token_path"] == steps(F(0))
    emitted = {k: field[k] for k in ("const_chain_id", "chain_id_path") if k in field}
    assert emitted == expected


@pytest.mark.parametrize(
    "params, feature",
    [
        ({"chainId": 1, "chainIdPath": "dstChain"}, "conflicting-params"),
        ({"chainId": 0}, "invalid-chain-id"),
        ({"chainId": -1}, "invalid-chain-id"),
        ({"chainId": 2**64}, "invalid-chain-id"),
        ({"chainId": "mainnet"}, "invalid-chain-id"),
        ({"chainId": True}, "invalid-chain-id"),
        ({"chainId": "$.metadata.constants.missing"}, "unresolvable-chain-id"),
        (
            {"chainId": {"$ref": "$.metadata.maps.dst", "keyPath": "@.chainId"}},
            "invalid-chain-id",
        ),
        ({"chainIdPath": "to"}, "unresolvable-chain-id-path"),
        ({"chainIdPath": "@.from"}, "unresolvable-chain-id-path"),
        ({"chainIdPath": "nope"}, "unresolvable-chain-id-path"),
        ({"token": "0x" + "ab" * 20}, "conflicting-params"),  # + tokenPath
    ],
    ids=[
        "both",
        "zero",
        "negative",
        "over-uint64",
        "non-numeric",
        "bool",
        "missing-constant",
        "map-ref",
        "path-address",
        "path-container-address",
        "path-unknown",
        "token-and-tokenPath",
    ],
)
def test_token_amount_bad_chain_id_skips_file(params, feature):
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(_token_chain_desc(params), unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {feature}


def test_chain_id_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(1)),
            "label": "Amount",
            "formatter": "FORMATTER_TOKEN_AMOUNT",
            "token_path": steps(F(0)),
            "chain_id_path": {"container_path": "CHAIN_ID"},
        }
    )
    assert info.chain_id_path.container_path == 4  # CHAIN_ID
    assert info.const_chain_id is None

    info = _build_erc7730_field_info(
        {
            "path": steps(F(1)),
            "label": "Amount",
            "formatter": "FORMATTER_TOKEN_AMOUNT",
            "const_token_address": "ab" * 20,
            "const_chain_id": 2**64 - 1,
        }
    )
    assert info.const_chain_id == 2**64 - 1
    assert info.chain_id_path is None


# =====================================================================
#                       enum formatter
# =====================================================================


def _enum_desc(params, enums=None, sig="f(uint8 mode)", path="mode"):
    return _descriptor(
        formats={
            sig: {
                "fields": [
                    {"path": path, "label": "Mode", "format": "enum", "params": params}
                ]
            }
        },
        enums=enums,
    )


def test_enum_field_emits_enum_values():
    # aave-style interest rate mode.
    desc = _enum_desc(
        {"$ref": "$.metadata.enums.interestRateMode"},
        enums={"interestRateMode": {"1": "Stable", "2": "Variable"}},
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_ENUM"
    assert field["enum_values"] == [
        {"key": 1, "value": "Stable"},
        {"key": 2, "value": "Variable"},
    ]
    # every accepted enum field is visible in the log
    assert [kind for _src, kind, _det in adjustments] == ["enum-field"]


def test_enum_bool_keys_map_to_1_0():
    # flyingtulip-style: True/False keys over a bool calldata value. On-device
    # the decoded bool compares equal to 1/0.
    desc = _enum_desc(
        {"$ref": "$.metadata.enums.rights"},
        enums={"rights": {"True": "Grant all", "False": "Deny all"}},
        sig="f(bool allow)",
        path="allow",
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert rec["field_definitions"][0]["enum_values"] == [
        {"key": 1, "value": "Grant all"},
        {"key": 0, "value": "Deny all"},
    ]
    assert {kind for _src, kind, _det in adjustments} == {
        "enum-bool-keys",
        "enum-field",
    }


def test_enum_missing_ref_skips_file():
    desc = _enum_desc({}, enums={"x": {"0": "A"}})
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"enum-missing-ref"}


def test_enum_unresolvable_ref_skips_file():
    desc = _enum_desc({"$ref": "$.metadata.enums.missing"}, enums={"other": {"0": "A"}})
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"unresolvable-enum-ref"}


def test_enum_non_numeric_key_skips_file():
    desc = _enum_desc(
        {"$ref": "$.metadata.enums.bad"}, enums={"bad": {"stable": "Stable"}}
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"invalid-enum-entry"}


def _enum_skip_reason(entries, sig="f(uint256 mode)"):
    desc = _enum_desc({"$ref": "$.metadata.enums.e"}, enums={"e": entries}, sig=sig)
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    return {feat for _src, feat, _det in unsupported}


def _enum_fields(entries, sig="f(uint256 mode)", path="mode"):
    """Return (enum_values, adjustment kinds) for one enum field over `entries`."""
    desc = _enum_desc(
        {"$ref": "$.metadata.enums.e"}, enums={"e": entries}, sig=sig, path=path
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    return rec["field_definitions"][0]["enum_values"], [k for _s, k, _d in adjustments]


def test_enum_wide_key_emits_wide_key_and_guard():
    values, kinds = _enum_fields({"1": "One", "4294967296": "Big"})
    assert values == [
        {"key": 1, "value": "One"},
        {"key": 0xFFFFFFFF, "wide_key": "0100000000", "value": "Big"},
        # guard: old firmware (no wide_key) sees a duplicate 0xFFFFFFFF key
        # and rejects the format instead of mislabelling calldata 0xFFFFFFFF
        {"key": 0xFFFFFFFF, "value": ""},
    ]
    assert kinds == ["enum-wide-keys", "enum-field"]


def test_enum_key_0xffffffff_is_wide():
    values, _ = _enum_fields({"0xFFFFFFFF": "Max32", "0xFFFFFFFE": "Below"})
    assert values == [
        {"key": 0xFFFFFFFF, "wide_key": "ffffffff", "value": "Max32"},
        {"key": 0xFFFFFFFE, "value": "Below"},
        {"key": 0xFFFFFFFF, "value": ""},
    ]


def test_enum_multiple_wide_keys_get_one_trailing_guard():
    max256 = str(2**256 - 1)
    values, _ = _enum_fields({max256: "Max", "0x1" + "00" * 8: "Mid", "0": "Zero"})
    assert values == [
        {"key": 0xFFFFFFFF, "wide_key": "ff" * 32, "value": "Max"},
        {"key": 0xFFFFFFFF, "wide_key": "01" + "00" * 8, "value": "Mid"},
        {"key": 0, "value": "Zero"},
        {"key": 0xFFFFFFFF, "value": ""},
    ]
    assert sum("wide_key" not in v and v["key"] == 0xFFFFFFFF for v in values) == 1


def test_enum_without_wide_keys_has_no_guard():
    values, kinds = _enum_fields({"0": "A", "0xFFFFFFFE": "B"})
    assert values == [{"key": 0, "value": "A"}, {"key": 0xFFFFFFFE, "value": "B"}]
    assert kinds == ["enum-field"]


def test_enum_registry_symbiotic_bytes32_roles():
    # OpenZeppelin AccessControl role ids are keccak256 hashes, so all but the
    # default admin role (0x00) are wide keys.
    # enum: https://github.com/ethereum/clear-signing-erc7730-registry/blob/7ba25fba5d7128930fb6683de99e0f0271f5e267/registry/symbiotic/calldata-UniversalDelegator-USDC.json#L48-L58
    # field: https://github.com/ethereum/clear-signing-erc7730-registry/blob/7ba25fba5d7128930fb6683de99e0f0271f5e267/registry/symbiotic/calldata-UniversalDelegator-USDC.json#L606-L634
    roles = {
        "0": "Default admin",
        "28243793089499064125200153067652308998873306605553429215694165115908920544912": "Allocate",
        "50560985435110027382313390568851860548267030219879002177657449806772213243098": "Deallocate",
        "88028664841565611520124839424531247632261927938012923435453451797300661270411": "Add adapter",
        "13264166804651107049971627042331359793689282905202948908894025771814477460686": "Swap adapter priority",
        "105913733732141619592880755283964529773244964259727493881968401745811500302826": "Remove adapter",
        "6873058840399056807680468190230370004676513393731428383794203968452095283877": "Force deallocate",
        "63526308245397922697810000530681480955750031233709255612285696458110986554933": "Set adapter limits",
        "58123140167707441986822184994193659285996543482856171670093325408374456504393": "Set allocation order",
    }
    desc = _enum_desc(
        {"$ref": "$.metadata.enums.roles"},
        enums={"roles": roles},
        sig="grantRole(bytes32 role,address account)",
        path="role.[0:32]",
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    [field] = rec["field_definitions"]
    values = field["enum_values"]

    assert field["path"] == steps(F(0), SL(0, 32))
    assert values[0] == {"key": 0, "value": "Default admin"}
    # 8 wide entries in descriptor order, then exactly one trailing guard
    assert len(values) == 1 + 8 + 1
    assert all(v["key"] == 0xFFFFFFFF and "wide_key" in v for v in values[1:-1])
    assert [v["value"] for v in values[1:-1]] == list(roles.values())[1:]
    assert values[-1] == {"key": 0xFFFFFFFF, "value": ""}
    # "Force deallocate" starts with a zero nibble, not a zero byte: still 32 bytes
    assert values[6] == {
        "key": 0xFFFFFFFF,
        "wide_key": "0f3203713f9a3f4a9748f1aee9bca713a30bc811b67a4b93434667911e0e92a5",
        "value": "Force deallocate",
    }
    assert [k for _s, k, _d in adjustments] == ["enum-wide-keys", "enum-field"]


def test_enum_registry_rarible_bytes4_asset_class_stays_narrow():
    # bytes4 asset-class ids all fit uint32, the largest being 0xF63C2825, so
    # there is no wide_key and no guard. The registry descriptor is EIP-712
    # (`AssetType(bytes4 assetClass,bytes data)`); a calldata stand-in with the
    # same bytes4 slice exercises the same key handling.
    # enum: https://github.com/ethereum/clear-signing-erc7730-registry/blob/7ba25fba5d7128930fb6683de99e0f0271f5e267/registry/rarible/eip712-rarible-exchange-v2.json#L15-L24
    # field: https://github.com/ethereum/clear-signing-erc7730-registry/blob/7ba25fba5d7128930fb6683de99e0f0271f5e267/registry/rarible/eip712-rarible-exchange-v2.json#L33-L38
    asset_class = {
        "2863578810": "ETH",
        "2330484100": "ERC20",
        "1940726086": "ERC721",
        "2537272896": "ERC1155",
        "4131137573": "Collection, any id",
        "1047235028": "CryptoPunk",
        "3640221889": "ERC721 lazy mint",
        "484420160": "ERC1155 lazy mint",
    }
    values, kinds = _enum_fields(
        asset_class, sig="f(bytes4 assetClass)", path="assetClass.[0:4]"
    )
    assert values == [{"key": int(k), "value": v} for k, v in asset_class.items()]
    assert {"key": 0xF63C2825, "value": "Collection, any id"} in values
    assert kinds == ["enum-field"]


def test_enum_key_wider_than_32_bytes_skips_file():
    assert _enum_skip_reason({str(2**256): "X"}) == {"invalid-enum-entry"}


def test_enum_negative_key_skips_file():
    assert _enum_skip_reason({"-1": "X"}) == {"invalid-enum-entry"}


@pytest.mark.parametrize(
    "entries",
    [
        {"1": "A", "0x1": "B"},
        {"true": "Yes", "1": "One"},
        {"4294967296": "A", "0x100000000": "B"},
    ],
)
def test_enum_duplicate_key_values_skip_file(entries):
    assert _enum_skip_reason(entries) == {"invalid-enum-entry"}


def test_enum_on_address_skips_file():
    desc = _enum_desc(
        {"$ref": "$.metadata.enums.x"},
        enums={"x": {"0": "A"}},
        sig="f(address a)",
        path="a",
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_enum_values_serialize_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "Mode",
            "formatter": "FORMATTER_ENUM",
            "enum_values": [{"key": 1, "value": "Stable"}],
        }
    )
    [entry] = info.enum_values
    assert (entry.key, entry.value) == (1, "Stable")


def test_enum_wide_key_serializes_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "Mode",
            "formatter": "FORMATTER_ENUM",
            "enum_values": [
                {"key": 0xFFFFFFFF, "wide_key": "0100000000", "value": "Big"},
                {"key": 0xFFFFFFFF, "value": ""},
            ],
        }
    )
    wide, guard = info.enum_values
    assert (wide.key, wide.wide_key, wide.value) == (
        0xFFFFFFFF,
        bytes.fromhex("0100000000"),
        "Big",
    )
    assert (guard.key, guard.wide_key, guard.value) == (0xFFFFFFFF, None, "")


# =====================================================================
#                       provider names
# =====================================================================


def test_provider_name_from_owner():
    desc = _descriptor(formats=_ADDR_FIELD)
    desc["metadata"]["owner"] = "Kiln"
    [rec] = build_display_formats(desc, source="kiln/calldata-Vault.json")
    assert rec["provider_name"] == "Kiln"


def test_provider_name_from_owner_and_contract_name():
    desc = _descriptor(formats=_ADDR_FIELD)
    desc["metadata"]["owner"] = "Lido DAO"
    desc["metadata"]["contractName"] = "wstETH"
    [rec] = build_display_formats(desc, source="lido/calldata-wstETH.json")
    assert rec["provider_name"] == "Lido DAO: wstETH"


@pytest.mark.parametrize(
    "owner, contract, expected",
    [
        ("WETH", "WETH", "WETH"),
        ("Celo", "Celo Governance", "Celo Governance"),
        ("Bedrock", "bedrock uniBTC Vault", "bedrock uniBTC Vault"),
        # Not a whole-word prefix: keep both.
        ("Sky", "Skyline", "Sky: Skyline"),
    ],
)
def test_provider_name_drops_owner_repeated_in_contract_name(owner, contract, expected):
    desc = _descriptor(formats=_ADDR_FIELD)
    desc["metadata"]["owner"] = owner
    desc["metadata"]["contractName"] = contract
    [rec] = build_display_formats(desc, source="x/calldata-X.json")
    assert rec["provider_name"] == expected


def test_provider_name_falls_back_to_subdir_and_contract_name():
    # 1inch-style: no owner, but a contractName and the registry subdirectory.
    desc = _descriptor(formats=_ADDR_FIELD)
    desc["metadata"]["contractName"] = "AggregationRouterV6"
    [rec] = build_display_formats(
        desc, source="1inch/calldata-AggregationRouterV6.json"
    )
    assert rec["provider_name"] == "1inch: AggregationRouterV6"


def test_provider_name_falls_back_to_subdir_alone():
    desc = _descriptor(formats=_ADDR_FIELD)
    [rec] = build_display_formats(desc, source="safe/calldata-Safe.json")
    assert rec["provider_name"] == "safe"


def test_provider_name_absent_when_nothing_known():
    # The default test source has no registry subdirectory.
    desc = _descriptor(formats=_ADDR_FIELD)
    [rec] = build_display_formats(desc)
    assert "provider_name" not in rec


def test_provider_name_serializes_to_proto():
    from ..serialize import serialize_display_format

    desc = _descriptor(formats=_ADDR_FIELD)
    desc["metadata"]["owner"] = "Kiln"
    [rec] = build_display_formats(desc, source="kiln/f.json")
    assert serialize_display_format(rec, 1234567890, 1)


# =====================================================================
#                       adjustment bookkeeping
# =====================================================================


def test_dropped_format_discards_its_adjustments():
    # Adjustments belong to *emitted* display formats only: a format that is
    # dropped later in the field loop must not leak its earlier adjustments.
    desc = _descriptor(
        formats={
            "good(address x)": {
                "fields": [{"path": "x", "label": "A", "format": "addressName"}]
            },
            "bad(uint256 y, bytes d)": {
                "fields": [
                    {"path": "y", "label": "Y", "format": "addressName"},  # adjustment
                    {"path": "d", "label": "D", "format": _UNSUPPORTED_FORMAT},  # drop
                ]
            },
        }
    )
    adjustments: list = []
    recs = build_display_formats(desc, adjustments=adjustments)
    assert len(recs) == 1
    assert adjustments == []


def test_nested_field_group_is_flattened():
    # A nested field group (Morpho Blue's `#.marketParams`) is flattened by
    # joining the sub-fields' relative paths onto the group's, and logged.
    desc = _descriptor(
        formats={
            "f((address loanToken, uint256 lltv) marketParams, uint256 assets)": {
                "fields": [
                    {
                        "path": "#.marketParams",
                        "fields": [
                            {
                                "path": "loanToken",
                                "label": "Loan Token",
                                "format": "addressName",
                            },
                            {"path": "lltv", "label": "LLTV", "format": "raw"},
                        ],
                    },
                    {"path": "assets", "label": "Assets", "format": "amount"},
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    fields = rec["field_definitions"]
    # display order preserved: group members first, then the sibling field
    assert [f["label"] for f in fields] == ["Loan Token", "LLTV", "Assets"]
    assert fields[0]["path"] == steps(F(0), F(0))
    assert fields[1]["path"] == steps(F(0), F(1))
    assert [k for _s, k, _d in adjustments] == ["nested-fields-flattened"]


def test_nested_group_within_group_is_flattened():
    desc = _descriptor(
        formats={
            "f((address a, uint256 b) outer)": {
                "fields": [
                    {
                        "path": "#.outer",
                        "fields": [
                            {
                                "path": "",
                                "fields": [
                                    {"path": "a", "label": "A", "format": "addressName"}
                                ],
                            }
                        ],
                    }
                ]
            }
        }
    )
    # inner group has empty base -> outer prefix already applied to it? No:
    # the inner group's own (empty) path passes members through with the
    # already-joined outer prefix intact.
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["path"] == steps(F(0), F(0))


def test_group_over_array_gives_per_element_fields():
    # Members of a group over an array (Morpho's `#.offerFills.[]`) and their
    # `*Path` params join onto the group's path, so they iterate too.
    desc = _descriptor(
        formats={
            "f((address t, uint256 a)[] swaps)": {
                "fields": [
                    {
                        "path": "#.swaps.[]",
                        "fields": [
                            {
                                "path": "a",
                                "label": "A",
                                "format": "tokenAmount",
                                "params": {"tokenPath": "t"},
                            }
                        ],
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), EACH, F(1))
    assert field["token_path"] == steps(F(0), EACH, F(0))


def _bundle_ids(sig: str, fields: list[dict[str, Any]]) -> dict[str, int | None]:
    [rec] = build_display_formats(_descriptor(formats={sig: {"fields": fields}}))
    return {f["label"]: f.get("bundle_id") for f in rec["field_definitions"]}


def test_bundled_groups_get_one_id_each():
    ids = _bundle_ids(
        "f((address a, uint256 b)[] xs, address[] ys, uint256[] zs)",
        [
            {
                "path": "#.xs.[]",
                "iteration": "bundled",
                "fields": [
                    {"path": "a", "label": "A", "format": "addressName"},
                    {"path": "b", "label": "B", "format": "raw"},
                ],
            },
            # No group path: bundles parallel arrays (Disperse, Surge).
            {
                "iteration": "bundled",
                "fields": [
                    {"path": "ys.[]", "label": "Y", "format": "addressName"},
                    {"path": "zs.[]", "label": "Z", "format": "raw"},
                ],
            },
        ],
    )
    assert ids == {"A": 1, "B": 1, "Y": 2, "Z": 2}


@pytest.mark.parametrize("iteration", ["sequential", None])
def test_unbundled_group_gets_no_bundle_id(iteration):
    group: dict[str, Any] = {
        "path": "#.xs.[]",
        "fields": [{"path": "a", "label": "A", "format": "addressName"}],
    }
    if iteration is not None:
        group["iteration"] = iteration
    assert _bundle_ids("f((address a)[] xs)", [group]) == {"A": None}


def test_bundle_id_only_on_iterating_fields():
    ids = _bundle_ids(
        "f(uint256[] xs, address to, (uint256[] ys) inner)",
        [
            {
                "iteration": "bundled",
                "fields": [
                    {"path": "xs.[]", "label": "X", "format": "raw"},
                    # a single value: nothing to show per element
                    {"path": "to", "label": "To", "format": "addressName"},
                    # a plain group inside the bundle stays in it
                    {
                        "path": "#.inner",
                        "fields": [{"path": "ys.[]", "label": "Y", "format": "raw"}],
                    },
                ],
            }
        ],
    )
    assert ids == {"X": 1, "To": None, "Y": 1}


def test_registry_morpho_bundled_collateral_params():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/4b7ff1c2df803f3e14f801bea9ecc779b6fa8b79/registry/morpho/calldata-Midnight.json#L165
    ids = _bundle_ids(
        "f((address loanToken, (address token, uint256 lltv, uint256"
        " liquidationCursor, address oracle)[] collateralParams) market)",
        [
            {
                "label": "Loan token",
                "format": "addressName",
                "path": "#.market.loanToken",
            },
            {
                "path": "#.market.collateralParams.[]",
                "iteration": "bundled",
                "fields": [
                    {
                        "label": "Collateral token",
                        "format": "addressName",
                        "path": "token",
                    },
                    {
                        "label": "LLTV",
                        "format": "unit",
                        "params": {"base": "%", "decimals": 16},
                        "path": "lltv",
                    },
                    {"label": "Oracle", "format": "addressName", "path": "oracle"},
                    {
                        "label": "Liquidation cursor",
                        "format": "raw",
                        "path": "liquidationCursor",
                        "visible": "never",
                    },
                ],
            },
        ],
    )
    assert ids == {"Loan token": None, "Collateral token": 1, "LLTV": 1, "Oracle": 1}


def test_bundle_id_serializes_to_proto():
    import io

    from trezorlib import protobuf

    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0), EACH),
            "label": "X",
            "formatter": "FORMATTER_RAW",
            "bundle_id": 3,
        }
    )
    buf = io.BytesIO()
    protobuf.dump_message(buf, info)
    buf.seek(0)
    assert protobuf.load_message(buf, type(info)).bundle_id == 3


def test_hidden_group_is_skipped():
    desc = _descriptor(
        formats={
            "f((address a, uint256 b) t, address to)": {
                "fields": [
                    {
                        "path": "#.t",
                        "visible": "never",
                        "fields": [
                            {"path": "a", "label": "A", "format": "addressName"}
                        ],
                    },
                    {"path": "to", "label": "To", "format": "addressName"},
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert [f["label"] for f in rec["field_definitions"]] == ["To"]


def test_optional_group_is_hidden_and_logged():
    desc = _descriptor(
        formats={
            "f((address a, uint256 b) t, address to)": {
                "fields": [
                    {
                        "path": "#.t",
                        "visible": "optional",
                        "fields": [
                            {"path": "a", "label": "A", "format": "addressName"}
                        ],
                    },
                    {"path": "to", "label": "To", "format": "addressName"},
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert [f["label"] for f in rec["field_definitions"]] == ["To"]
    assert [k for _s, k, _d in adjustments] == ["optional-field-hidden"]


def test_group_member_path_params_are_joined_too():
    # paraswap `#.swapData`: sub-fields reference sibling members via
    # relative path params (`tokenPath: srcToken`) — joined like `path`.
    desc = _descriptor(
        formats={
            "f((address srcToken, uint256 fromAmount) swapData)": {
                "fields": [
                    {
                        "path": "#.swapData",
                        "fields": [
                            {
                                "path": "fromAmount",
                                "label": "Amount to Send",
                                "format": "tokenAmount",
                                "params": {"tokenPath": "srcToken"},
                            }
                        ],
                    }
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    [field] = rec["field_definitions"]
    assert field["path"] == steps(F(0), F(1))
    assert field["token_path"] == steps(F(0), F(0))


def test_group_member_with_absolute_path_is_not_joined():
    desc = _descriptor(
        formats={
            "f((uint256 x) t, address to)": {
                "fields": [
                    {
                        "path": "#.t",
                        "fields": [
                            {"path": "#.to", "label": "To", "format": "addressName"}
                        ],
                    },
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["field_definitions"][0]["path"] == steps(F(1))


def test_bad_format_is_skipped_clean_formats_kept():
    # First function is fine; the second has an unsupported formatter. Only the
    # bad display format is dropped — the good one is still emitted — and the
    # bad one's feature is recorded.
    desc = _descriptor(
        formats={
            "good(address x)": {
                "fields": [{"path": "x", "label": "Addr", "format": "addressName"}]
            },
            "bad(uint256 y)": {
                "fields": [{"path": "y", "label": "Y", "format": _UNSUPPORTED_FORMAT}]
            },
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
            "good(address x)": {
                "fields": [{"path": "x", "label": "Addr", "format": "addressName"}]
            },
            "bad1(uint256 y)": {
                "fields": [{"path": "y", "label": "Amt", "format": _UNSUPPORTED_FORMAT}]
            },
            "bad2(uint256 z)": {
                "fields": [{"path": "z", "label": "Amt", "format": _UNSUPPORTED_FORMAT}]
            },
        }
    )
    recs = build_display_formats(desc)
    assert len(recs) == 1
    assert recs[0]["field_definitions"][0]["label"] == "Addr"


def test_all_formats_bad_skips_whole_file():
    # When no display format survives, the whole file is skipped (raises).
    desc = _descriptor(
        formats={
            "bad1(uint256 y)": {
                "fields": [{"path": "y", "label": "Y", "format": _UNSUPPORTED_FORMAT}]
            },
            "bad2(uint256 z)": {
                "fields": [{"path": "z", "label": "Z", "format": _UNSUPPORTED_FORMAT}]
            },
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


def test_optional_visibility_hides_field_and_logs():
    # `visible: optional` = "wallets MAY display this field" (ERC-7730):
    # hiding is spec-compliant — the field is skipped, the format survives,
    # and the choice is visible in the log.
    desc = _descriptor(
        formats={
            "f(address to, uint256 fee)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {
                        "path": "fee",
                        "label": "Fee",
                        "format": "amount",
                        "visible": "optional",
                    },
                ]
            }
        }
    )
    adjustments: list = []
    [rec] = build_display_formats(desc, adjustments=adjustments)
    assert [f["label"] for f in rec["field_definitions"]] == ["To"]
    assert [kind for _src, kind, _det in adjustments] == ["optional-field-hidden"]


def test_optional_visibility_with_bad_path_does_not_skip_file():
    # Like other hidden fields, an optional field is skipped before any
    # path/formatter validation.
    desc = _descriptor(
        formats={
            "f(address to)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {
                        "path": "nope.[0:2].x",
                        "label": "X",
                        "format": "amount",
                        "visible": "optional",
                    },
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert len(rec["field_definitions"]) == 1


def _w(n: int) -> str:
    """`n` as a 32-byte ABI word (hex), how integer/bool rule values encode."""
    return (n % 2**256).to_bytes(32, "big").hex()


def _rule_field(sig: str, field: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """Build a one-format descriptor and return its single emitted field."""
    [rec] = build_display_formats(_descriptor(formats={sig: {"fields": [field]}}, **kw))
    [out] = rec["field_definitions"]
    return out


def test_if_not_in_is_emitted_with_encoded_values():
    # The paraswap / DAI-permit shape: hide an expiry of 0. Numeric values
    # (number, decimal or hex string) encode as minimal sign-preserving
    # two's complement, and duplicates collapse.
    adjustments: list = []
    [rec] = build_display_formats(
        _descriptor(
            formats={
                "f(uint256 expiry)": {
                    "fields": [
                        {
                            "path": "expiry",
                            "label": "Valid until",
                            "format": "date",
                            "params": {"encoding": "timestamp"},
                            "visible": {"ifNotIn": [0, "0", "0x100"]},
                        }
                    ]
                }
            }
        ),
        adjustments=adjustments,
    )
    [field] = rec["field_definitions"]
    assert field["formatter"] == "FORMATTER_DATE"
    assert field["if_not_in"] == [_w(0), _w(256)]  # 0x100 = 256
    assert "must_match" not in field
    # tests logs:
    assert ("conditional-visibility") in {k for _s, k, _d in adjustments}


@pytest.mark.parametrize(
    "sig, path, values, expected",
    [
        (
            "f(address a)",
            "a",
            ["0x" + "AB" * 20, 0],
            ["ab" * 20, "00" * 20],
        ),
        ("f(bool b)", "b", [True, "false", 1], [_w(1), _w(0)]),
        ("f(bytes4 s)", "s", ["0xdeadbeef", 0], ["deadbeef", "00000000"]),
        ("f(bytes d)", "d", ["0x", "0x01"], ["", "01"]),
        # numeric 0 on dynamic bytes = empty (lifi nonEVMReceiver); dedups with "0x"
        ("f(bytes d)", "d", [0, "0x", "0"], [""]),
        ("f(string s)", "s", ["", "hi"], ["", "6869"]),
        ("f(uint256 x)", "x.[-4:]", [0], ["00000000"]),
        ("f(uint256 x)", "x.[-20:]", ["0x" + "cd" * 20], ["cd" * 20]),
        # the 32-byte ABI word; two's complement for negatives
        (
            "f(uint8 x)",
            "x",
            [0, 255],
            ["00" * 32, "00" * 31 + "ff"],
        ),
        (
            "f(uint256 x)",
            "x",
            [2**256 - 1],
            ["ff" * 32],
        ),
        (
            "f(int160 x)",
            "x",
            [5, -1, -2, "-2"],
            ["00" * 31 + "05", "ff" * 32, "ff" * 31 + "fe"],
        ),
        (
            "f(int160 x)",
            "x",
            [-(2**159), 2**159 - 1],
            ["ff" * 12 + "80" + "00" * 19, "00" * 12 + "7f" + "ff" * 19],
        ),
        ("f()", "@.value", [0], [_w(0)]),
        ("f()", "@.to", ["0x" + "11" * 20], ["11" * 20]),
    ],
)
def test_rule_values_encode_by_leaf_type(sig, path, values, expected):
    field = _rule_field(
        sig,
        {"path": path, "label": "L", "format": "raw", "visible": {"ifNotIn": values}},
    )
    assert field["if_not_in"] == expected


def _device_bytes(v: Any) -> bytes:
    """The firmware side of the contract (proposed for clear_signing.py):
    the walked field value as bytes, checked for membership in the entries."""
    if isinstance(v, str):
        return v.encode()
    if isinstance(v, int):  # uint, int, bool, @.value
        return (v % (1 << 256)).to_bytes(32, "big")
    return v


@pytest.mark.parametrize(
    "sig, path, value, on_device",
    [
        # (descriptor value, the Python value the firmware holds after walking)
        ("f(uint8 x)", "x", "255", 255),
        ("f(int160 x)", "x", -2, -2),
        ("f(int24 x)", "x", -(2**23), -(2**23)),
        ("f(int256 x)", "x", 2**255 - 1, 2**255 - 1),
        ("f(uint216 x)", "x", 2**216 - 1, 2**216 - 1),
        ("f(bool b)", "b", False, False),
        ("f(bool b)", "b", 1, True),
        ("f(address a)", "a", "0x" + "AB" * 20, bytes.fromhex("ab" * 20)),
        ("f(bytes4 s)", "s", "0xdeadbeef", bytes.fromhex("deadbeef")),
        ("f(bytes d)", "d", "0x", b""),
        ("f(bytes d)", "d", 0, b""),
        ("f(string s)", "s", "erc20", "erc20"),
        ("f(uint256 x)", "x.[-4:]", 0, b"\x00" * 4),
        ("f()", "@.value", "0x10", 16),
    ],
)
def test_rule_entries_match_device_bytes_view(sig, path, value, on_device):
    field = _rule_field(
        sig,
        {"path": path, "label": "L", "format": "raw", "visible": {"ifNotIn": [value]}},
    )
    assert [bytes.fromhex(e) for e in field["if_not_in"]] == [_device_bytes(on_device)]


def test_rule_values_follow_address_retype():
    # addressName over a uint retypes the leaf to address — rule values must
    # then encode as 20-byte addresses, not minimal integers.
    field = _rule_field(
        "f(uint256 x)",
        {
            "path": "x",
            "label": "L",
            "format": "addressName",
            "visible": {"ifNotIn": [0]},
        },
    )
    assert field["if_not_in"] == ["00" * 20]


def test_rule_values_resolve_constants():
    field = _rule_field(
        "f(address a)",
        {
            "path": "a",
            "label": "L",
            "format": "addressName",
            "visible": {"ifNotIn": ["$.metadata.constants.zero"]},
        },
        constants={"zero": "0x" + "00" * 20},
    )
    assert field["if_not_in"] == ["00" * 20]


def test_must_match_is_emitted_as_raw_even_without_format():
    # mustMatch fields are never shown, but the firmware must check them:
    # emitted as RAW (params dropped), label falling back to the path.
    adjustments: list = []
    [rec] = build_display_formats(
        _descriptor(
            formats={
                "f(address to, address token, uint256 v)": {
                    "fields": [
                        {"path": "to", "label": "To", "format": "addressName"},
                        {"path": "token", "visible": {"mustMatch": ["0x" + "22" * 20]}},
                        {
                            "path": "v",
                            "label": "V",
                            "format": "tokenAmount",
                            "params": {"tokenPath": "token"},
                            "visible": {"mustMatch": [1, 2]},
                        },
                    ]
                }
            }
        ),
        adjustments=adjustments,
    )
    to, token, v = rec["field_definitions"]
    assert "must_match" not in to
    assert token == {
        "path": steps(F(1)),
        "label": "token",
        "formatter": "FORMATTER_RAW",
        "must_match": ["22" * 20],
    }
    assert v["formatter"] == "FORMATTER_RAW"
    assert v["must_match"] == [_w(1), _w(2)]
    assert "token_path" not in v
    assert "must-match-as-raw" in {k for _s, k, _d in adjustments}


def test_if_not_in_without_format_stays_hidden():
    [rec] = build_display_formats(
        _descriptor(
            formats={
                "f(address to, uint256 x)": {
                    "fields": [
                        {"path": "to", "label": "To", "format": "addressName"},
                        {"path": "x", "label": "X", "visible": {"ifNotIn": [0]}},
                    ]
                }
            }
        )
    )
    assert [f["label"] for f in rec["field_definitions"]] == ["To"]


@pytest.mark.parametrize(
    "sig, path, visible, feature",
    [
        # malformed rule objects
        (
            "f(uint256 x)",
            "x",
            {"ifNotIn": [0], "mustMatch": [0]},
            "conditional-visibility",
        ),
        ("f(uint256 x)", "x", {"ifNotIn": []}, "conditional-visibility"),
        ("f(uint256 x)", "x", {"ifNotIn": 0}, "conditional-visibility"),
        ("f(uint256 x)", "x", {"showIf": [0]}, "conditional-visibility"),
        # values that have no encoding for the leaf
        ("f(uint256 x)", "x", {"ifNotIn": [None]}, "invalid-visibility-value"),
        ("f(uint256 x)", "x", {"ifNotIn": [-1]}, "invalid-visibility-value"),
        ("f(uint8 x)", "x", {"ifNotIn": [256]}, "invalid-visibility-value"),
        ("f(int160 x)", "x", {"ifNotIn": [2**159]}, "invalid-visibility-value"),
        ("f(int160 x)", "x", {"ifNotIn": [-(2**159) - 1]}, "invalid-visibility-value"),
        ("f(int24 x)", "x", {"ifNotIn": [2**23]}, "invalid-visibility-value"),
        ("f(uint216 x)", "x", {"ifNotIn": [2**216]}, "invalid-visibility-value"),
        ("f(int256 x)", "x", {"ifNotIn": [-(2**255) - 1]}, "invalid-visibility-value"),
        ("f()", "@.value", {"ifNotIn": [-1]}, "invalid-visibility-value"),
        ("f(uint256 x)", "x", {"ifNotIn": ["abc"]}, "invalid-visibility-value"),
        ("f(address a)", "a", {"ifNotIn": ["0x1234"]}, "invalid-visibility-value"),
        ("f(bool b)", "b", {"ifNotIn": [2]}, "invalid-visibility-value"),
        ("f(bytes4 s)", "s", {"ifNotIn": ["0xdead"]}, "invalid-visibility-value"),
        # dynamic bytes: only 0 has a width-free meaning
        ("f(bytes d)", "d", {"ifNotIn": [1]}, "invalid-visibility-value"),
        ("f(bytes d)", "d", {"ifNotIn": [False]}, "invalid-visibility-value"),
        ("f(string s)", "s", {"ifNotIn": [0]}, "invalid-visibility-value"),
        (
            "f(uint256 x)",
            "x",
            {"ifNotIn": ["$.metadata.constants.nope"]},
            "unresolvable-visibility-value",
        ),
    ],
)
def test_bad_visibility_rule_drops_format(sig, path, visible, feature):
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _descriptor(
                formats={
                    sig: {
                        "fields": [
                            {
                                "path": path,
                                "label": "L",
                                "format": "raw",
                                "visible": visible,
                            }
                        ]
                    }
                }
            ),
            unsupported=unsupported,
        )
    assert {feat for _src, feat, _det in unsupported} == {feature}


def test_visibility_rule_on_constant_field_drops_format():
    # A constant's value is static; a rule on it is not folded away.
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _descriptor(
                formats={
                    "f(address to)": {
                        "fields": [
                            {"path": "to", "label": "To", "format": "addressName"},
                            {
                                "label": "C",
                                "format": "raw",
                                "value": "x",
                                "visible": {"ifNotIn": ["y"]},
                            },
                        ]
                    }
                }
            ),
            unsupported=unsupported,
        )
    assert {feat for _src, feat, _det in unsupported} == {"conditional-visibility"}


def test_visibility_rule_on_group_still_drops_format():
    # A group's "value" is a whole tuple — no single leaf to compare.
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _descriptor(
                formats={
                    "f((address a, uint256 b) p)": {
                        "fields": [
                            {
                                "path": "p",
                                "visible": {"ifNotIn": [0]},
                                "fields": [
                                    {"path": "a", "label": "A", "format": "raw"}
                                ],
                            }
                        ]
                    }
                }
            ),
            unsupported=unsupported,
        )
    assert {feat for _src, feat, _det in unsupported} == {"conditional-visibility"}


# ---------------------------------------------------------------------
# Registry shapes: one case per distinct kind of rule found in the
# registry (leaf type x rule x how the values are spelled). Signatures are
# trimmed to the parameters that matter; EIP-712 shapes are recast as
# calldata, the parser's only input.
# ---------------------------------------------------------------------

_ZERO_ADDR = "0x" + "00" * 20
_ZERO_WORD = "0x" + "00" * 32


@pytest.mark.parametrize(
    "sig, field, rule, expected",
    [
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/10102/calldata-TimeLockRouter.json#L14
        pytest.param(
            "f()",
            {
                "path": "@.value",
                "label": "ETH",
                "format": "amount",
                "visible": {"ifNotIn": [0]},
            },
            "if_not_in",
            [_w(0)],
            id="value-ifNotIn-int",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/morpho/calldata-BlueBundlesV1.json#L62
        pytest.param(
            "f()",
            {
                "path": "@.value",
                "label": "ETH",
                "format": "amount",
                "visible": {"ifNotIn": ["0"]},
            },
            "if_not_in",
            [_w(0)],
            id="value-ifNotIn-decimal-string",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/sushi/calldata-RedSnwapper.json#L28
        pytest.param(
            "f()",
            {
                "path": "@.value",
                "label": "ETH Sent",
                "format": "amount",
                "visible": {"mustMatch": [0]},
            },
            "must_match",
            [_w(0)],
            id="value-mustMatch",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/10102/calldata-TimeLockRouter.json#L20
        pytest.param(
            "f((address storageToken, uint256 amountOutMin) s)",
            {
                "path": "s.storageToken",
                "label": "Token",
                "format": "addressName",
                "params": {"types": ["token"]},
                "visible": {"ifNotIn": [_ZERO_ADDR]},
            },
            "if_not_in",
            ["00" * 20],
            id="address-ifNotIn-zero",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ocarina-trade/calldata-OTCRegistry.json#L141
        pytest.param(
            "f(((address offerer, address zone) components) reg)",
            {
                "path": "#.reg.components.zone",
                "label": "Registry",
                "format": "raw",
                "visible": {
                    "mustMatch": [
                        "0x07C0000007b4B558e2fCd47F47A573413B0Caf7C",
                        "0xfc9E05BF732FB5Aeee7e270928F349Ed3FA3cc0D",
                    ]
                },
            },
            "must_match",
            [
                "07c0000007b4b558e2fcd47f47a573413b0caf7c",
                "fc9e05bf732fb5aeee7e270928f349ed3fa3cc0d",
            ],
            id="address-mustMatch-checksummed",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/base/calldata-OptimismPortal2.json#L19
        pytest.param(
            "depositTransaction(address _to,uint256 _value,uint64 _gasLimit,"
            "bool _isCreation,bytes _data)",
            {
                "path": "_isCreation",
                "label": "Contract Creation",
                "format": "raw",
                "visible": {"mustMatch": [False]},
            },
            "must_match",
            [_w(0)],
            id="bool-mustMatch-false",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/across/calldata-Ethereum_SpokePool.json#L91
        pytest.param(
            "f(address recipient, bytes message)",
            {
                "path": "message",
                "label": "Message",
                "format": "raw",
                "visible": {"mustMatch": ["0x"]},
            },
            "must_match",
            [""],
            id="bytes-mustMatch-empty",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/lifi/calldata-LIFIDiamond.json#L635
        pytest.param(
            "f((bytes nonEVMReceiver, bytes32 solanaATA) _ecoData)",
            {
                "path": "_ecoData.solanaATA",
                "label": "Solana Token Account",
                "format": "raw",
                "visible": {"ifNotIn": [0]},
            },
            "if_not_in",
            ["00" * 32],
            id="bytes32-ifNotIn-int",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ocarina-trade/calldata-OTCRegistry.json#L154
        pytest.param(
            "f(((bytes32 conduitKey, uint256 salt) components) reg)",
            {
                "path": "#.reg.components.conduitKey",
                "label": "Conduit key",
                "format": "raw",
                "visible": {"mustMatch": [_ZERO_WORD]},
            },
            "must_match",
            ["00" * 32],
            id="bytes32-mustMatch-hex",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ocarina-trade/calldata-OTCRegistry.json#L135
        pytest.param(
            "f((uint256 id, string memo) reg)",
            {
                "path": "#.reg.memo",
                "label": "Memo",
                "format": "raw",
                "visible": {"ifNotIn": [""]},
            },
            "if_not_in",
            [""],
            id="string-ifNotIn-empty",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ophis/eip712-Ophis-GPv2Settlement.json#L48 (EIP-712)
        pytest.param(
            "f(string sellTokenBalance)",
            {
                "path": "sellTokenBalance",
                "label": "Sell: Balancer Vault",
                "format": "raw",
                "visible": {"ifNotIn": ["erc20", "internal"]},
            },
            "if_not_in",
            ["erc20".encode().hex(), "internal".encode().hex()],
            id="string-ifNotIn-text",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ophis/calldata-Ophis-CoWSwapEthFlow.json#L45
        pytest.param(
            "f((uint256 sellAmount, uint256 feeAmount) order)",
            {
                "path": "order.feeAmount",
                "label": "Order max fee",
                "format": "amount",
                "visible": {"ifNotIn": [0, "0"]},
            },
            "if_not_in",
            [_w(0)],
            id="uint-ifNotIn-int-and-string",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/paraswap/eip712-paraswap.json#L27 (EIP-712)
        pytest.param(
            "f(uint128 expiry)",
            {
                "path": "expiry",
                "label": "Expiration time",
                "format": "date",
                "params": {"encoding": "timestamp"},
                "visible": {"ifNotIn": [0, "0"]},
            },
            "if_not_in",
            [_w(0)],
            id="uint128-date-ifNotIn",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/morpho/calldata-BlueBundlesV1.json#L144
        pytest.param(
            "f((address authorizer, uint256 deadline) signedAuthorization)",
            {
                "path": "#.signedAuthorization.deadline",
                "label": "Deadline",
                "format": "date",
                "params": {"encoding": "timestamp"},
                "visible": {"ifNotIn": ["0"]},
            },
            "if_not_in",
            [_w(0)],
            id="uint-date-ifNotIn-string",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/10102/calldata-TransferEOALegacyRouter.json#L46
        pytest.param(
            "f((uint256 delayLayer2, uint256 delayLayer3) extraConfig_)",
            {
                "path": "extraConfig_.delayLayer2",
                "label": "Backup delay",
                "format": "duration",
                "visible": {"ifNotIn": [0]},
            },
            "if_not_in",
            [_w(0)],
            id="uint-duration-ifNotIn",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/morpho/calldata-BlueBundlesV1.json#L89
        pytest.param(
            "f(address token, uint256 collateralAssets)",
            {
                "path": "#.collateralAssets",
                "label": "Collateral",
                "format": "tokenAmount",
                "params": {"tokenPath": "token"},
                "visible": {"ifNotIn": ["0"]},
            },
            "if_not_in",
            [_w(0)],
            id="uint-tokenAmount-ifNotIn",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/morpho/calldata-BlueBundlesV1.json#L123
        pytest.param(
            "f(uint256 maxLtv)",
            {
                "path": "#.maxLtv",
                "label": "Max LTV",
                "format": "unit",
                "params": {"base": "%", "decimals": 16},
                "visible": {"ifNotIn": [str(2**256 - 1)]},
            },
            "if_not_in",
            [_w(2**256 - 1)],
            id="uint-unit-ifNotIn-max",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/morpho/calldata-BlueBundlesV1.json#L189
        pytest.param(
            "f(uint256 repayShares)",
            {
                "path": "#.repayShares",
                "label": "Repay shares",
                "format": "raw",
                "visible": {"ifNotIn": ["0"]},
            },
            "if_not_in",
            [_w(0)],
            id="uint-raw-ifNotIn",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/uniswap/calldata-UniswapV3Router02.json#L46
        pytest.param(
            "f((address tokenIn, uint160 sqrtPriceLimitX96) params)",
            {
                "path": "params.sqrtPriceLimitX96",
                "label": "Price limit",
                "format": "raw",
                "visible": {"mustMatch": [0]},
            },
            "must_match",
            [_w(0)],
            id="uint160-mustMatch",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/tether/calldata-usdt0-OFT-Flare.json#L52
        pytest.param(
            "f((uint256 nativeFee, uint256 lzTokenFee) _fee)",
            {
                "label": "LZ token fee",
                "path": "#._fee.lzTokenFee",
                "visible": {"mustMatch": [0]},
                "format": "raw",
            },
            "must_match",
            [_w(0)],
            id="uint-mustMatch-int",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/flare/calldata-FXRP-OFT-Flare.json#L52
        pytest.param(
            "f((uint256 nativeFee, uint256 lzTokenFee) _fee)",
            {
                "label": "LZ token fee",
                "path": "#._fee.lzTokenFee",
                "visible": {"mustMatch": ["0"]},
                "format": "raw",
            },
            "must_match",
            [_w(0)],
            id="uint-mustMatch-string",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ocarina-trade/calldata-OTCRegistry.json#L126
        pytest.param(
            "f(((uint8 orderType, uint256 startTime) components) reg)",
            {
                "path": "#.reg.components.orderType",
                "label": "Order type",
                "format": "raw",
                "visible": {"mustMatch": ["2"]},
            },
            "must_match",
            [_w(2)],
            id="uint8-mustMatch-string",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/enso/calldata-EnsoRouter.json#L22
        pytest.param(
            "f((uint8 tokenType, bytes data) tokenIn)",
            {
                "path": "#.tokenIn.tokenType",
                "label": "Token type",
                "format": "raw",
                "visible": {"mustMatch": [1]},
            },
            "must_match",
            [_w(1)],
            id="uint8-mustMatch-int",
        ),
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/lifi/calldata-LIFIDiamond.json#L560
        # `0` on dynamic bytes = unset = empty; LiFi's facets check
        # `nonEVMReceiver.length == 0`. An all-zero payload stays shown.
        pytest.param(
            "f((bytes nonEVMReceiver, uint32 dstToken) _chainflipData)",
            {
                "path": "_chainflipData.nonEVMReceiver",
                "label": "Non-EVM Recipient",
                "format": "raw",
                "visible": {"ifNotIn": [0]},
            },
            "if_not_in",
            [""],
            id="dynamic-bytes-ifNotIn-int",
        ),
    ],
)
def test_registry_rule_shapes(sig, field, rule, expected):
    out = _rule_field(sig, field)
    assert out[rule] == expected


def test_registry_rule_on_enum():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/across/calldata-Ethereum_SpokePool.json#L65
    out = _rule_field(
        "f(uint256 destinationChainId)",
        {
            "path": "destinationChainId",
            "label": "Destination Chain",
            "format": "enum",
            "params": {"$ref": "$.metadata.enums.destinationChainOverride"},
            "visible": {"ifNotIn": [1, 10, 56, 137, 324, 8453, 42161]},
        },
        enums={"destinationChainOverride": {"130": "Unichain"}},
    )
    assert out["formatter"] == "FORMATTER_ENUM"
    assert out["if_not_in"] == [_w(n) for n in (1, 10, 56, 137, 324, 8453, 42161)]


def test_registry_rule_override_on_ref():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/ophis/calldata-Ophis-CoWSwapEthFlow.json#L32
    # The rule sits on the referencing field and overrides the definition.
    [rec] = build_display_formats(
        _descriptor(
            formats={
                "f((uint256 sellAmount, uint256 feeAmount) order)": {
                    "fields": [
                        {
                            "path": "order.feeAmount",
                            "$ref": "$.display.definitions.feeAmount",
                            "visible": {"ifNotIn": [0, "0"]},
                        }
                    ]
                }
            },
            definitions={"feeAmount": {"label": "Fee", "format": "amount"}},
        )
    )
    [out] = rec["field_definitions"]
    assert (out["label"], out["if_not_in"]) == ("Fee", [_w(0)])


@pytest.mark.parametrize(
    "sig, field, feature",
    [
        # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/pendle/calldata-PendleRouterV4.json#L1625
        # The firmware checks a rule against one value, not per element.
        pytest.param(
            "f((address target, bool allowFailure, bytes callData)[] calls)",
            {
                "path": "calls.[].allowFailure",
                "label": "Allow failure",
                "format": "raw",
                "visible": {"mustMatch": [False]},
            },
            "conditional-visibility",
            id="per-element-path",
        ),
        pytest.param(
            "f(uint256[] xs)",
            {
                "path": "xs.[]",
                "label": "Xs",
                "format": "raw",
                "visible": {"ifNotIn": [7]},
            },
            "conditional-visibility",
            id="whole-array-path",
        ),
    ],
)
def test_registry_rule_shapes_that_drop(sig, field, feature):
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(
            _descriptor(formats={sig: {"fields": [field]}}), unsupported=unsupported
        )
    assert {feat for _src, feat, _det in unsupported} == {feature}


def test_chain_id_format_with_if_not_in():
    # similar to https://github.com/ethereum/clear-signing-erc7730-registry/blob/3d332d5fb832b78e4745ee6c7046a0c2ea100426/registry/across/calldata-Ethereum_SpokePool.json#L43
    [rec] = build_display_formats(
        _descriptor(
            formats={
                "f(uint256 destinationChainId)": {
                    "fields": [
                        {
                            "path": "destinationChainId",
                            "label": "Destination Chain",
                            "format": "chainId",
                            "visible": {"ifNotIn": [130, 143]},
                        }
                    ]
                }
            }
        )
    )
    [out] = rec["field_definitions"]
    assert out["formatter"] == "FORMATTER_CHAIN_ID"
    assert out["if_not_in"] == [_w(130), _w(143)]


def test_visibility_rules_serialize_to_proto():
    from ..serialize import _build_erc7730_field_info

    info = _build_erc7730_field_info(
        {
            "path": steps(F(0)),
            "label": "L",
            "formatter": "FORMATTER_RAW",
            "if_not_in": ["00", "0100"],
            "must_match": ["ab" * 20],
        }
    )
    assert info.if_not_in == [b"\x00", b"\x01\x00"]
    assert info.must_match == [bytes.fromhex("ab" * 20)]


def test_hidden_field_with_format_but_visible_never_is_skipped():
    # A field can carry a `format` yet be hidden via visible:never — still not
    # displayed, so an unsupported formatter on it must not skip the file.
    desc = _descriptor(
        formats={
            "f(address to, bytes raw)": {
                "fields": [
                    {"path": "to", "label": "To", "format": "addressName"},
                    {
                        "path": "raw",
                        "label": "R",
                        "format": _UNSUPPORTED_FORMAT,
                        "visible": "never",
                    },
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
    assert field["token_path"] == steps(F(0))


# =====================================================================
#                       build_abi_value — tuples & arrays
# =====================================================================


def test_build_abi_value_atomic_and_dynamic():
    assert build_abi_value(_component("f(uint256 x)")) == {"atomic": "ABI_UINT256"}
    assert build_abi_value(_component("f(bytes x)")) == {"dynamic": "ABI_BYTES"}
    assert build_abi_value(_component("f(uint256[] x)")) == {
        "array": {"atomic": "ABI_UINT256"}
    }


def test_build_abi_value_bytes20_and_int160():
    # Added by the bytesN-types firmware branch (ABI_BYTES20 / ABI_INT160).
    assert build_abi_value(_component("f(bytes20 x)")) == {"atomic": "ABI_BYTES20"}
    assert build_abi_value(_component("f(int160 x)")) == {"atomic": "ABI_INT160"}


@pytest.mark.parametrize("bits", [24, 32, 64, 128, 160, 256])
def test_build_abi_value_signed_ints(bits):
    # Every signed width the firmware's `_get_parser` decodes.
    sig = f"f(int{bits} x)"
    assert build_abi_value(_component(sig)) == {"atomic": f"ABI_INT{bits}"}
    assert path_to_dict("x", _inputs(sig))[1] == KIND_NUMERIC


@pytest.mark.parametrize(
    "bits", [8, 16, 24, 32, 40, 48, 64, 72, 96, 112, 120, 128, 160, 216, 248, 256]
)
def test_build_abi_value_unsigned_ints(bits):
    # Every unsigned width the firmware's `_get_parser` decodes.
    sig = f"f(uint{bits} x)"
    assert build_abi_value(_component(sig)) == {"atomic": f"ABI_UINT{bits}"}
    assert path_to_dict("x", _inputs(sig))[1] == KIND_NUMERIC


def test_build_abi_value_unsupported_unsigned_int():
    # A width the firmware doesn't decode stays unrepresentable.
    with pytest.raises(ValueError, match="unknown ABI type"):
        build_abi_value(_component("f(uint208 x)"))


def test_build_abi_value_unsupported_signed_int():
    # A width the firmware doesn't decode stays unrepresentable.
    with pytest.raises(ValueError, match="unknown ABI type"):
        build_abi_value(_component("f(int8 x)"))


def test_bytes20_and_int160_kinds():
    # bytes20 is a bytes-like scalar (raw/addressName render it); int160
    # decodes to an int on-device, so it's numeric.
    assert path_to_dict("x", _inputs("f(bytes20 x)"))[1] == KIND_BYTES
    assert path_to_dict("x", _inputs("f(int160 x)"))[1] == KIND_NUMERIC


def test_bytes20_tuple_field_end_to_end():
    # tBTC-style: a bytes20 wallet key hash inside a reveal tuple, shown raw.
    desc = _descriptor(
        formats={
            "requestRedemption(bytes20 walletPubKeyHash, uint64 amount)": {
                "fields": [
                    {"path": "walletPubKeyHash", "label": "Wallet", "format": "raw"},
                    {"path": "amount", "label": "Amount", "format": "amount"},
                ]
            }
        }
    )
    [rec] = build_display_formats(desc)
    assert rec["parameter_definitions"] == [
        {"atomic": "ABI_BYTES20"},
        {"atomic": "ABI_UINT64"},
    ]


def test_addressname_on_int160_skips_file():
    # int160 is numeric but signed — the uint->address retype does not apply.
    desc = _descriptor(
        formats={
            "f(int160 x)": {
                "fields": [{"path": "x", "label": "L", "format": "addressName"}]
            }
        }
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, unsupported=unsupported)
    assert {feat for _src, feat, _det in unsupported} == {"formatter-type-mismatch"}


def test_build_abi_value_tuple_dynamism_is_truthful():
    # A tuple with a dynamic field is itself dynamic.
    v = build_abi_value(_component("f((address a, bytes data) t)"))
    assert v["tuple"]["is_dynamic"] is True
    # An all-static tuple is static.
    v = build_abi_value(_component("f((address a, uint256 n) t)"))
    assert v["tuple"]["is_dynamic"] is False
    # Including inside an array. We used to emit `False` here to match a
    # firmware that parsed in-array tuples as static; neither firmware ever
    # read the flag in that position (the old one recomputed it from the
    # fields, the new one ignores it everywhere), so emit the truth.
    v = build_abi_value(_component("f((address a, bytes data)[] xs)"))
    assert v == {
        "array": {
            "tuple": {
                "fields": [{"atomic": "ABI_ADDRESS"}, {"dynamic": "ABI_BYTES"}],
                "is_dynamic": True,
            }
        }
    }
    # Dynamism propagates up through nesting: a tuple holding a static tuple
    # that holds a `bytes` is dynamic all the way out.
    v = build_abi_value(_component("f((uint256 n, (address a, bytes d) inner) t)"))
    assert v["tuple"]["is_dynamic"] is True


def test_build_abi_value_fixed_size_array_is_unsupported():
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f(uint256[2] xs)"))
    assert exc.value.feature == "fixed-size-array"

    # Also inside a tuple. This used to report `non-leaf-tuple-field`, because
    # the non-leaf scan tested `endswith("]")` and caught it first.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((uint256[2] xs, address a) t)"))
    assert exc.value.feature == "fixed-size-array"


def test_build_abi_value_empty_tuple_is_unsupported():
    # Solidity has no zero-field struct, but the signature parser will produce
    # one. The firmware rejects it: a static tuple with head_size 0 inside an
    # array defeats the `array_length * head_size` bounds pre-check.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f(() t)"))
    assert exc.value.feature == "empty-tuple"


def test_build_abi_value_unknown_type_raises_value_error():
    # Not an UnsupportedFeature: a type the firmware's `_get_parser` grew and
    # `_ABI_TYPE_MAP` did not is our bug, logged as `unrepresentable-params`.
    # The caps must not mask it, so the check runs first.
    with pytest.raises(ValueError, match="unknown ABI type"):
        build_abi_value(_component("f(int8[][][] x)"))


# ---------------------------------------------------------------------
# Nesting caps
#
# Two caps mirrored from the firmware's `ABIValue.from_proto`, bounding two
# different resources. `_MAX_NESTED_ARRAYS` (2) bounds parse *work*: a tuple
# parses each field once so it adds, an array parses its element subtree once
# per element so it multiplies, and the element count is attacker-supplied.
# `_MAX_ABI_NESTING` (8) bounds the recursion itself. The firmware side of
# these is pinned by `test_depth_cap_boundary` and
# `test_three_nested_arrays_rejected`; keep the two sets in step.
# ---------------------------------------------------------------------


def _nest_tuples(n: int, inner: Component | None = None) -> Component:
    """`n` tuples wrapped around `inner` (a `uint256` leaf by default)."""
    c = inner or Component(name="leaf", type="uint256")
    for i in range(n):
        c = Component(name=f"t{i}", type="tuple", components=[c])
    return c


def test_build_abi_value_two_deep_array_is_kept():
    assert build_abi_value(_component("f(uint256[][] x)")) == {
        "array": {"array": {"atomic": "ABI_UINT256"}}
    }


def test_build_abi_value_three_deep_array_is_unsupported():
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f(uint256[][][] x)"))
    assert exc.value.feature == "array-nesting-too-deep"


def test_build_abi_value_arrays_through_a_tuple_still_accumulate():
    # The array counter follows the root-to-leaf path, not the top level:
    # burying three array levels under a tuple does not launder them past the
    # cap. Mirrors the firmware's test_three_nested_arrays_through_tuple_rejected.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_component("f((uint256[][][] x) t)"))
    assert exc.value.feature == "array-nesting-too-deep"


def test_build_abi_value_sibling_arrays_do_not_accumulate():
    # Two arrays in one tuple sit on separate paths, so each carries one array
    # level, not two between them. Sibling arrays add work; only nesting
    # multiplies it.
    assert build_abi_value(_component("f((uint256[] a, uint256[] b) t)")) == {
        "tuple": {
            "fields": [
                {"array": {"atomic": "ABI_UINT256"}},
                {"array": {"atomic": "ABI_UINT256"}},
            ],
            "is_dynamic": True,
        }
    }


def test_build_abi_value_depth_cap_boundary():
    # The check is `depth > 8` with the root at depth 0, so 8 wrapping tuples
    # (leaf at depth 8) is the deepest accepted and 9 is one too many. Pure
    # tuple nesting costs no parse work at all, which is why the array cap
    # cannot stand in for this one.
    assert build_abi_value(_nest_tuples(8)) is not None

    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_nest_tuples(9))
    assert exc.value.feature == "abi-nesting-too-deep"


def test_build_abi_value_depth_and_array_caps_interact():
    # The two counters advance independently, so a tree can sit on both caps at
    # once: 6 tuples plus `uint256[][]` puts the leaf at depth 8, arrays 2.
    arrays = Component(name="x", type="uint256[][]")
    assert build_abi_value(_nest_tuples(6, arrays)) is not None

    # One more tuple and it is the depth cap, not the array cap, that fires.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(_nest_tuples(7, arrays))
    assert exc.value.feature == "abi-nesting-too-deep"


# ---------------------------------------------------------------------
# Shapes from the registry
#
# The real ERC-7730 descriptor shapes the recursion exists to unblock, named
# for the contract each came from so a regression names its victim. Each has a
# twin in the firmware's own suite (core/tests/test_apps.ethereum.clear_signing.py,
# "shapes from the registry") which asserts the same shape decodes; these
# assert the parser will actually emit it.
# ---------------------------------------------------------------------


def test_build_abi_value_serenita_leaf_array_inside_struct():
    # serenita / p2p `updateState`: a struct with a `bytes32[]` member. Every
    # field of that descriptor is `visible: never` — it was dropped purely
    # because the parameter could not be typed.
    assert build_abi_value(
        _component(
            "f((bytes32 rewardsRoot, int160 reward, uint160 unlockedMevReward,"
            " bytes32[] proof) harvestParams)"
        )
    ) == {
        "tuple": {
            "fields": [
                {"atomic": "ABI_BYTES32"},
                {"atomic": "ABI_INT160"},
                {"atomic": "ABI_UINT160"},
                {"array": {"atomic": "ABI_BYTES32"}},
            ],
            "is_dynamic": True,
        }
    }


def test_build_abi_value_kyberswap_struct_in_struct():
    # kyberswap `swap` reads *through* nesting: its visible fields are
    # `execution.desc.amount` / `.minReturnAmount` / `.dstReceiver`, with
    # tokenPath `execution.desc.srcToken`. Assert the type tree and the index
    # paths together — the firmware test hardcodes exactly these tuples.
    sig = (
        "swap((address callTarget, address approveTarget, bytes targetData,"
        " (address srcToken, address dstToken, address[] srcReceivers,"
        " uint256[] srcAmounts, address[] feeReceivers, uint256[] feeAmounts,"
        " address dstReceiver, uint256 amount, uint256 minReturnAmount,"
        " uint256 flags, bytes permit) desc, bytes clientData) execution)"
    )
    v = build_abi_value(_component(sig))
    desc = v["tuple"]["fields"][3]
    assert desc["tuple"]["fields"][0] == {"atomic": "ABI_ADDRESS"}  # srcToken
    assert desc["tuple"]["fields"][2] == {"array": {"atomic": "ABI_ADDRESS"}}
    assert desc["tuple"]["fields"][7] == {"atomic": "ABI_UINT256"}  # amount

    inputs = _inputs(sig)
    assert path_to_dict("execution.desc.amount", inputs) == (
        steps(F(0), F(3), F(7)),
        KIND_NUMERIC,
    )
    assert path_to_dict("execution.desc.srcToken", inputs) == (
        steps(F(0), F(3), F(0)),
        KIND_ADDRESS,
    )
    assert path_to_dict("execution.desc.dstReceiver", inputs) == (
        steps(F(0), F(3), F(6)),
        KIND_ADDRESS,
    )


def test_build_abi_value_okx_dag_swap_sits_on_the_array_cap():
    # okx `dagSwapByOrderId`s `paths`: two array levels — the outer array plus
    # the arrays inside the element struct — so it is exactly on the cap.
    assert build_abi_value(
        _component(
            "f((address[] mixAdapters, address[] assetTo, uint256[] rawData,"
            " bytes[] extraData, uint256 fromToken)[] paths)"
        )
    ) == {
        "array": {
            "tuple": {
                "fields": [
                    {"array": {"atomic": "ABI_ADDRESS"}},
                    {"array": {"atomic": "ABI_ADDRESS"}},
                    {"array": {"atomic": "ABI_UINT256"}},
                    {"array": {"dynamic": "ABI_BYTES"}},
                    {"atomic": "ABI_UINT256"},
                ],
                "is_dynamic": True,
            }
        }
    }


def test_build_abi_value_okx_smart_swap_is_over_the_array_cap():
    # okx `smartSwapByOrderId`s `batches` is the same element under one more
    # array: three levels, so the whole display format is dropped. This is the
    # deferred case — no field ever reads the parameter, so it would come back
    # if unreferenced parameters were pruned.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(
            _component(
                "f((address[] mixAdapters, address[] assetTo, uint256[] rawData,"
                " bytes[] extraData, uint256 fromToken)[][] batches)"
            )
        )
    assert exc.value.feature == "array-nesting-too-deep"


def test_build_abi_value_paraswap_mega_swap_is_over_the_array_cap():
    # paraswap `megaSwap` / `multiSwap`: `path` -> `adapters` -> `route` is
    # three nested arrays of structs, so it stays dropped.
    with pytest.raises(UnsupportedFeature) as exc:
        build_abi_value(
            _component(
                "f((address fromToken, uint256 fromAmount, uint256 toAmount,"
                " uint256 expectedAmount, address beneficiary,"
                " (uint256 fromAmountPercent, (address to, uint256 totalNetworkFee,"
                " (address adapter, uint256 percent, uint256 networkFee,"
                " (uint256 index, address targetExchange, uint256 percent,"
                " bytes payload, uint256 networkFee)[] route)[] adapters)[] path)[]"
                " path, address partner, uint256 feePercent, bytes permit,"
                " uint256 deadline, bytes16 uuid) data)"
            )
        )
    assert exc.value.feature == "array-nesting-too-deep"


def test_build_abi_value_flare_nested_static_struct_in_array():
    # flare `initialiseWeightBasedClaims`: a struct holding both a leaf array
    # and a nested *static* struct. The static struct occupies four words
    # inline, which is the shape the firmware's old fixed one-word field stride
    # got wrong.
    sig = (
        "f((bytes32[] merkleProof, (uint24 rewardEpochId, bytes20 beneficiary,"
        " uint120 amount, uint8 claimType) body)[] proofs)"
    )
    body = {
        "tuple": {
            "fields": [
                {"atomic": "ABI_UINT24"},
                {"atomic": "ABI_BYTES20"},
                {"atomic": "ABI_UINT120"},
                {"atomic": "ABI_UINT8"},
            ],
            "is_dynamic": False,
        }
    }
    assert build_abi_value(_component(sig)) == {
        "array": {
            "tuple": {
                "fields": [{"array": {"atomic": "ABI_BYTES32"}}, body],
                "is_dynamic": True,
            }
        }
    }
    # The index path reaches the grandchild leaf, matching the firmware's
    # decoded `[([proof_leaf], (7, beneficiary, 99, 1))]`.
    assert path_to_dict("proofs.[0].body.amount", _inputs(sig)) == (
        steps(F(0), IX(0), F(1), F(2)),
        KIND_NUMERIC,
    )


def test_build_abi_value_flyingtulip_static_tuple_in_array():
    # flyingtulip `createSession`s `limits`. An all-static element struct is
    # encoded in place at a 64-byte stride, with no offset table; we used to
    # refuse to emit it for fear the firmware would misread it.
    assert build_abi_value(
        _component("f((address token, uint256 limit)[] limits)")
    ) == {
        "array": {
            "tuple": {
                "fields": [{"atomic": "ABI_ADDRESS"}, {"atomic": "ABI_UINT256"}],
                "is_dynamic": False,
            }
        }
    }


def test_build_abi_value_struct_field_may_be_an_array_of_structs():
    # No supported descriptor has this shape, but the recursion admits it and
    # the static element struct gives the array a 64-byte stride.
    assert build_abi_value(
        _component("f((uint256 a, (address b, uint256 c)[] xs) t)")
    ) == {
        "tuple": {
            "fields": [
                {"atomic": "ABI_UINT256"},
                {
                    "array": {
                        "tuple": {
                            "fields": [
                                {"atomic": "ABI_ADDRESS"},
                                {"atomic": "ABI_UINT256"},
                            ],
                            "is_dynamic": False,
                        }
                    }
                },
            ],
            "is_dynamic": True,
        }
    }


def test_build_abi_value_leaf_only_tuple_in_two_arrays():
    # `tuple[][]` is fine as long as the element struct holds no array of its
    # own: the two array levels are all there are. It was rejected outright
    # before, when a tuple was allowed in at most one array.
    assert build_abi_value(_component("f((address a, bytes d)[][] xs)")) == {
        "array": {
            "array": {
                "tuple": {
                    "fields": [{"atomic": "ABI_ADDRESS"}, {"dynamic": "ABI_BYTES"}],
                    "is_dynamic": True,
                }
            }
        }
    }


# =====================================================================
#                 metadata.maps — whole file skipped
# =====================================================================


def _with_maps(desc: dict[str, Any], maps: Any) -> dict[str, Any]:
    desc["metadata"]["maps"] = maps
    return desc


def test_metadata_maps_skips_whole_file_and_logs():
    # The ERC-7730 spec's own maps example; the clean `good` format is dropped
    # too — the file is skipped whole, whether the maps are used or not.
    desc = _with_maps(
        _descriptor(
            formats={
                "good(address x)": {
                    "fields": [{"path": "x", "label": "X", "format": "addressName"}]
                },
                "deposit(uint256 amount)": {
                    "fields": [
                        {
                            "path": "#.amount",
                            "label": "Deposit Amount",
                            "format": "tokenAmount",
                            "params": {
                                "token": {
                                    "map": "$.metadata.maps.underlyingToken",
                                    "keyPath": "@.chainId",
                                }
                            },
                        }
                    ]
                },
            }
        ),
        {
            "underlyingToken": {
                "$keyType": "Chain ID",
                "values": {"1": "0x" + "aa" * 20, "17000": "0x" + "bb" * 20},
            }
        },
    )
    unsupported: list = []
    with pytest.raises(UnsupportedFeature):
        build_display_formats(desc, source="x.json", unsupported=unsupported)
    assert unsupported == [
        ("x.json", "metadata-maps", "x.json: declares metadata.maps (underlyingToken)")
    ]
    # Without a collector the file is still skipped; the reason rides in the
    # exception.
    with pytest.raises(UnsupportedFeature, match="declares metadata.maps"):
        build_display_formats(desc, source="x.json")


def test_empty_metadata_maps_is_ignored():
    formats = {
        "f(uint256 a)": {"fields": [{"path": "a", "label": "A", "format": "amount"}]}
    }
    unsupported: list = []
    recs = build_display_formats(
        _with_maps(_descriptor(formats), {}), unsupported=unsupported
    )
    assert recs == build_display_formats(_descriptor(formats))
    assert unsupported == []
