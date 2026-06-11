import copy
from typing import Any

import pytest

from .erc7730 import build_display_formats
from .erc7730_validate import (
    ValidationError,
    _resolve_container,
    _resolve_indices,
    derive_expected,
    validate_file,
    validate_file_strict,
)


def _inputs(signature: str) -> list:
    from erc7730.common.abi import parse_signature

    return list(parse_signature(signature).inputs or [])


def _descriptor(
    formats: dict[str, Any],
    definitions: dict[str, Any] | None = None,
    deployments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    display: dict[str, Any] = {"formats": formats}
    if definitions is not None:
        display["definitions"] = definitions
    if deployments is None:
        deployments = [{"chainId": 1, "address": "0x" + "11" * 20}]
    return {
        "context": {"contract": {"deployments": deployments}},
        "display": display,
        "metadata": {"constants": {}},
    }


def _swap_descriptor() -> dict[str, Any]:
    # The tuple carries a dynamic `bytes callData` field so the whole tuple is
    # dynamic — required for it to be valid inside an array (mirrors LiFi).
    sig = (
        "swap(address _receiver, uint256 _minOut, "
        "(address sendingAssetId, address receivingAssetId, uint256 fromAmount, "
        "bytes callData)[] _swapData)"
    )
    return _descriptor(
        {
            sig: {
                "intent": "Swap",
                "fields": [
                    {
                        "path": "_swapData.[0].fromAmount",
                        "label": "Amount to Send",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "_swapData.[0].sendingAssetId"},
                        "visible": "always",
                    },
                    {
                        "path": "_minOut",
                        "label": "Minimum to Receive",
                        "format": "tokenAmount",
                        "params": {"tokenPath": "_swapData.[-1].receivingAssetId"},
                        "visible": "always",
                    },
                    {
                        "path": "_receiver",
                        "label": "Recipient",
                        "format": "addressName",
                        "visible": "always",
                    },
                    {"path": "_minOut", "label": "hidden", "visible": "never"},
                ],
            }
        },
        deployments=[
            {"chainId": 1, "address": "0x" + "11" * 20},
            {"chainId": 10, "address": "0x" + "22" * 20},
        ],
    )


# =====================================================================
#                       _resolve_indices
# =====================================================================


def test_resolve_indices_nested_array():
    inputs = _inputs(
        "f((address sendingAssetId, address receivingAssetId, uint256 fromAmount)[] _swapData)"
    )
    assert _resolve_indices("_swapData.[0].fromAmount", inputs) == [0, 0, 2]
    assert _resolve_indices("_swapData.[-1].receivingAssetId", inputs) == [0, -1, 1]


def test_resolve_indices_unsupported_returns_none():
    inputs = _inputs("f(uint256 amount, address to)")
    assert _resolve_indices("@.value", inputs) is None  # container
    assert _resolve_indices("$.metadata.constants.x", inputs) is None  # descriptor
    assert _resolve_indices("unknownName", inputs) is None  # unknown segment
    assert _resolve_indices("amount.[]", inputs) is None  # whole-array iteration


def test_resolve_container():
    assert _resolve_container("@.value") == "VALUE"
    assert _resolve_container("@.from") == "FROM"
    assert _resolve_container("@.to") == "TO"
    assert _resolve_container("_receiver") is None  # data path
    assert _resolve_container("@.unknown") is None  # unmapped container field


def _native_descriptor() -> dict[str, Any]:
    return _descriptor(
        {
            "pay(address _to)": {
                "intent": "Send",
                "fields": [
                    {"path": "@.value", "label": "Amount", "format": "amount"},
                    {
                        "path": "_to",
                        "label": "To",
                        "format": "addressName",
                        "visible": "always",
                    },
                ],
            }
        }
    )


def test_validate_container_path_clean_passes():
    desc = _native_descriptor()
    records = build_display_formats(desc, source="test")
    assert validate_file("test", desc, records, print_summary=False) == []


def test_wrong_container_path_raises():
    desc = _native_descriptor()
    records = build_display_formats(desc, source="test")
    records[0]["field_definitions"][0]["path"] = {"container_path": "TO"}
    with pytest.raises(ValidationError, match="container_path"):
        validate_file_strict("test", desc, records, print_summary=False)


# =====================================================================
#                       happy path
# =====================================================================


def test_validate_clean_passes():
    desc = _swap_descriptor()
    records = build_display_formats(desc, source="test")
    # 1 signature x 2 deployments
    assert len(records) == 2
    errors = validate_file("test", desc, records, print_summary=False)
    assert errors == []
    validate_file_strict("test", desc, records, print_summary=False)  # no raise


def test_derive_expected_skips_hidden_field():
    desc = _swap_descriptor()
    expected = derive_expected(desc)
    (ef,) = expected.values()
    # 3 visible fields; the `visible: never` one is dropped.
    assert [f.label for f in ef.fields] == [
        "Amount to Send",
        "Minimum to Receive",
        "Recipient",
    ]
    assert ef.intent == "Swap"


# =====================================================================
#                       mismatch detection
# =====================================================================


@pytest.fixture
def desc_and_records():
    desc = _swap_descriptor()
    records = build_display_formats(desc, source="test")
    return desc, records


def _mutated(records, fn):
    recs = copy.deepcopy(records)
    fn(recs)
    return recs


def test_dropped_field_raises(desc_and_records):
    desc, records = desc_and_records
    recs = _mutated(records, lambda r: r[0]["field_definitions"].pop())
    with pytest.raises(ValidationError, match="fields emitted"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_wrong_label_raises(desc_and_records):
    desc, records = desc_and_records
    recs = _mutated(
        records, lambda r: r[0]["field_definitions"][0].__setitem__("label", "X")
    )
    with pytest.raises(ValidationError, match="label"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_wrong_formatter_raises(desc_and_records):
    desc, records = desc_and_records
    recs = _mutated(
        records,
        lambda r: r[0]["field_definitions"][0].__setitem__(
            "formatter", "FORMATTER_AMOUNT"
        ),
    )
    with pytest.raises(ValidationError, match="formatter"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_wrong_path_raises(desc_and_records):
    desc, records = desc_and_records
    recs = _mutated(
        records,
        lambda r: r[0]["field_definitions"][0]["path"].__setitem__("path", [9]),
    )
    with pytest.raises(ValidationError, match="path"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_missing_token_path_raises(desc_and_records):
    desc, records = desc_and_records
    recs = _mutated(
        records, lambda r: r[0]["field_definitions"][0].pop("token_path")
    )
    with pytest.raises(ValidationError, match="token_path"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_missing_deployment_raises(desc_and_records):
    desc, records = desc_and_records
    recs = _mutated(records, lambda r: r.pop())  # drop one of two deployments
    with pytest.raises(ValidationError, match="deployments emitted"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_extra_selector_raises(desc_and_records):
    desc, records = desc_and_records
    recs = copy.deepcopy(records)
    bogus = copy.deepcopy(recs[0])
    bogus["func_sig"] = "0xdeadbeef"
    recs.append(bogus)
    with pytest.raises(ValidationError, match="not present in descriptor"):
        validate_file_strict("test", desc, recs, print_summary=False)


def test_resolve_ref_ignores_non_definitions_paths():
    # Must mirror the extractor: only `$.display.definitions.<key>` resolves;
    # a bare string or a `$.metadata.*` path is ignored (not key-sniffed).
    from .erc7730_validate import _resolve_ref

    defs = {"foo": {"label": "Foo", "format": "amount"}}
    for ref in ("foo", "$.metadata.enums.foo", "$.display.definitions.foo.bar"):
        field = {"$ref": ref, "path": "x"}
        assert _resolve_ref(field, defs) == field  # unchanged

    # the well-formed path still resolves
    merged = _resolve_ref({"$ref": "$.display.definitions.foo", "path": "x"}, defs)
    assert merged["label"] == "Foo"
