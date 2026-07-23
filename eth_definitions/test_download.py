from copy import deepcopy

from . import download as dl
from .download import (
    _dedup_display_formats,
    _force_networks_fields_sizes_t1,
    _force_tokens_fields_sizes_t1,
    _write_display_formats_log,
)
from .test_data import networks, erc20_tokens


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
        [("provA/f.json", [a]), ("provB/g.json", [b])], {1}
    )
    assert len(conflicts) == 1
    _key, overridden, kept = conflicts[0]
    assert overridden == "provA/f.json"
    assert kept == "provB/g.json"
    assert emitted == [b]  # later file wins


def test_dedup_identical_definitions_not_a_conflict():
    emitted, conflicts = _dedup_display_formats(
        [("x.json", [_rec()]), ("y.json", [_rec()])], {1}
    )
    assert conflicts == []
    assert len(emitted) == 1


def test_dedup_ignores_unknown_chains():
    emitted, conflicts = _dedup_display_formats(
        [("x.json", [_rec(chain_id=999)])], {1}
    )
    assert emitted == []
    assert conflicts == []


def test_write_display_formats_log_has_all_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DISPLAY_FORMATS_LOG_PATH", tmp_path / "out.log")
    _write_display_formats_log(
        unsupported=[("provA/f.json", "unsupported-formatter", "enum (field 'X')")],
        conflicts=[("chain=1 address=0xabc selector=0xdead", "provA/f.json", "provB/g.json")],
        adjustments=[
            ("provC/h.json", "calldata-as-raw", "data shown as raw bytes (field 'Swap')")
        ],
    )
    text = (tmp_path / "out.log").read_text()
    assert text.startswith("# Providers blocked: ")
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
