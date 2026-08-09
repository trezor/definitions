import json
from copy import deepcopy

import click
import pytest

from ..test_data import erc20_tokens, networks
from . import load as dl
from .load import (
    _dedup_display_formats,
    _force_networks_fields_sizes_t1,
    _force_tokens_fields_sizes_t1,
    _write_display_formats_log,
)


def _rec(chain_id=1, address="0x" + "11" * 20, func_sig="0xdeadbeef", intent="Swap"):
    return {
        "chain_id": chain_id,
        "address": address,
        "func_sig": func_sig,
        "intent": intent,
        "parameter_definitions": [],
        "field_definitions": [],
    }


# ====== display-format dedup / override-conflict detection ======


def test_dedup_detects_conflicting_override():
    a = _rec(intent="Swap")
    b = _rec(intent="Exchange")  # same key, different payload
    emitted, conflicts = _dedup_display_formats(
        [("provA/f.json", True, [a]), ("provB/g.json", True, [b])], {1}
    )
    assert len(conflicts) == 1
    _key, overridden, kept = conflicts[0]
    assert overridden == "provA/f.json"
    assert kept == "provB/g.json"
    assert emitted == [b]  # later file wins


def test_dedup_identical_definitions_not_a_conflict():
    emitted, conflicts = _dedup_display_formats(
        [("x.json", True, [_rec()]), ("y.json", True, [_rec()])], {1}
    )
    assert conflicts == []
    assert len(emitted) == 1


def test_dedup_conflicts_span_gated_and_ungated_but_only_gated_emitted():
    ungated = _rec(intent="A")
    gated = _rec(intent="B")
    emitted, conflicts = _dedup_display_formats(
        [("other/f.json", False, [ungated]), ("lifi/g.json", True, [gated])], {1}
    )
    assert len(conflicts) == 1
    assert emitted == [gated]  # not-enabled provider does not feed output


def test_dedup_ignores_unknown_chains():
    emitted, conflicts = _dedup_display_formats(
        [("x.json", True, [_rec(chain_id=999)])], {1}
    )
    assert emitted == []
    assert conflicts == []


def test_write_display_formats_log_has_all_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DISPLAY_FORMATS_LOG_PATH", tmp_path / "out.log")
    _write_display_formats_log(
        unsupported=[("provA/f.json", "unsupported-formatter", "enum (field 'X')")],
        conflicts=[
            ("chain=1 address=0xabc selector=0xdead", "provA/f.json", "provB/g.json")
        ],
        adjustments=[
            (
                "provC/h.json",
                "calldata-as-raw",
                "data shown as raw bytes (field 'Swap')",
            )
        ],
    )
    text = (tmp_path / "out.log").read_text()
    assert text.startswith("# Providers enabled: ")
    assert "unsupported features" in text
    assert "unsupported-formatter" in text
    assert "Adjustments" in text
    assert "calldata-as-raw" in text
    assert "provC/h.json" in text
    assert "Conflicting overrides" in text
    assert "kept:     provB/g.json" in text
    assert "overrode: provA/f.json" in text


def test_force_tokens_fields_sizes_t1_no_change():
    # No change
    all_tokens = deepcopy(erc20_tokens)
    _force_tokens_fields_sizes_t1(all_tokens)
    assert all_tokens == erc20_tokens


def test_force_tokens_fields_sizes_t1_value_error():
    # Invalid address - ValueError
    all_tokens = deepcopy(erc20_tokens)
    all_tokens[0]["address"] += "0"
    _force_tokens_fields_sizes_t1(all_tokens)
    # First token is missing
    assert len(all_tokens) == len(erc20_tokens) - 1
    assert all_tokens[:] == erc20_tokens[1:]


def test_force_tokens_fields_sizes_t1_longer_address():
    # Invalid address - longer than 20 bytes
    all_tokens = deepcopy(erc20_tokens)
    bad_index = 1
    all_tokens[bad_index]["address"] += "00"
    _force_tokens_fields_sizes_t1(all_tokens)
    # Bad index is missing, all others are the same
    assert len(all_tokens) == len(erc20_tokens) - 1
    assert all_tokens[:bad_index] == erc20_tokens[:bad_index]
    assert all_tokens[bad_index:] == erc20_tokens[bad_index + 1 :]


def test_force_tokens_fields_sizes_t1_two_invalid():
    # Invalid address - two are bad, checking correct deletion
    all_tokens = deepcopy(erc20_tokens)
    all_tokens[1]["address"] += "0"
    all_tokens[3]["address"] += "00"
    _force_tokens_fields_sizes_t1(all_tokens)
    # Two tokens are missing
    assert len(all_tokens) == len(erc20_tokens) - 2
    popped_tokens = deepcopy(erc20_tokens)
    popped_tokens.pop(3)
    popped_tokens.pop(1)
    assert all_tokens == popped_tokens


def test_force_tokens_fields_sizes_t1_name_over_limit():
    # Name over limit
    all_tokens = deepcopy(erc20_tokens)
    all_tokens[0]["name"] = "a" * 512
    _force_tokens_fields_sizes_t1(all_tokens)
    # Name shortened
    assert all_tokens[0]["name"] == "a" * 256
    assert len(all_tokens) == len(erc20_tokens)
    assert all_tokens[1:] == erc20_tokens[1:]


def test_force_tokens_fields_sizes_t1_shortcut_over_limit():
    # Shortcut over limit
    all_tokens = deepcopy(erc20_tokens)
    all_tokens[0]["shortcut"] = "b" * 512
    _force_tokens_fields_sizes_t1(all_tokens)
    # Shortcut shortened
    assert all_tokens[0]["shortcut"] == "b" * 256
    assert len(all_tokens) == len(erc20_tokens)
    assert all_tokens[1:] == erc20_tokens[1:]


def test_force_networks_fields_sizes_t1_name_over_limit():
    # Name over limit
    all_networks = deepcopy(networks)
    all_networks[0]["name"] = "a" * 512
    _force_networks_fields_sizes_t1(all_networks)
    # Name shortened
    assert all_networks[0]["name"] == "a" * 256
    assert len(all_networks) == len(networks)
    assert all_networks[1:] == networks[1:]


def test_force_networks_fields_sizes_t1_shortcut_over_limit():
    # Shortcut over limit
    all_networks = deepcopy(networks)
    all_networks[0]["shortcut"] = "b" * 512
    _force_networks_fields_sizes_t1(all_networks)
    # Shortcut shortened
    assert all_networks[0]["shortcut"] == "b" * 256
    assert len(all_networks) == len(networks)
    assert all_networks[1:] == networks[1:]


# ====== erc7730-only refresh ======


def test_update_display_formats_only_preserves_other_sections(tmp_path, monkeypatch):
    old = {
        "networks": [{"chain_id": 1, "name": "keep-me"}],
        "erc20_tokens": [{"chain_id": 1, "address": "0xabc"}],
        "solana_tokens": [{"mint": "keep-mint"}],
        "erc20_display_formats": [_rec(intent="OLD")],
    }
    defs_path = tmp_path / "definitions-latest.json"
    defs_path.write_text(json.dumps(old))
    monkeypatch.setattr(dl, "DEFINITIONS_PATH", defs_path)

    new_formats = [_rec(intent="NEW")]
    monkeypatch.setattr(
        dl, "_load_display_formats_from_repo", lambda networks: list(new_formats)
    )
    # The diff/apply step is exercised elsewhere; here we assert the section swap.
    monkeypatch.setattr(dl, "check_definitions_list", lambda **kwargs: None)
    monkeypatch.setattr(dl, "make_metadata", lambda data: {"meta": "x"})

    captured = {}
    monkeypatch.setattr(
        dl,
        "store_definitions_data",
        lambda metadata, definitions_data, **kw: captured.update(data=definitions_data),
    )

    dl._update_display_formats_only(
        networks=[{"chain_id": 1}],
        change_strategy=None,
        show_all=False,
        show_added=False,
    )

    data = captured["data"]
    # networks / tokens / Solana are reused verbatim from the existing file
    assert data.networks == old["networks"]
    assert data.erc20_tokens == old["erc20_tokens"]
    assert data.solana_tokens == old["solana_tokens"]
    # only the display formats are refreshed
    assert data.erc20_display_formats == new_formats


def test_update_display_formats_only_requires_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DEFINITIONS_PATH", tmp_path / "missing.json")
    with pytest.raises(click.ClickException):
        dl._update_display_formats_only(
            networks=[], change_strategy=None, show_all=False, show_added=False
        )
