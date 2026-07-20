from copy import deepcopy
from unittest import mock

import pytest

from . import download as dl
from .download import (
    _dedup_display_formats,
    _force_networks_fields_sizes_t1,
    _force_tokens_fields_sizes_t1,
    _load_robinhood_tokens_from_registry,
    _merge_erc20_token_sources,
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


def _robinhood_asset(
    symbol: str,
    name: str,
    address: str,
    chain_id: int = 4663,
):
    return {
        "tokenSymbol": symbol,
        "tokenName": name,
        "deployments": [{"contractAddress": address, "chainId": chain_id}],
        "status": "ASSET_STATUS_ACTIVE",
    }


# ====== official Robinhood token registry ======


def test_get_robinhood_assets_uses_cached_download():
    assets = [
        _robinhood_asset("AAPL", "Apple • Robinhood Token", "0x" + "11" * 20)
    ]
    downloader = mock.Mock()
    downloader._download_json.return_value = {"assets": assets}

    assert dl.Downloader.get_robinhood_assets(downloader) == assets
    downloader._download_json.assert_called_once_with(dl.ROBINHOOD_ASSETS_URL)


@pytest.mark.parametrize("payload", ({}, {"assets": []}, {"assets": None}))
def test_get_robinhood_assets_rejects_empty_payload(payload):
    downloader = mock.Mock()
    downloader._download_json.return_value = payload

    with pytest.raises(ValueError, match="returned no assets"):
        dl.Downloader.get_robinhood_assets(downloader)


def test_load_robinhood_tokens_from_registry():
    downloader = mock.Mock()
    downloader.get_robinhood_assets.return_value = [
        _robinhood_asset(
            "AAPL",
            "Apple • Robinhood Token",
            "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9",
        ),
        _robinhood_asset(
            "SKHY",
            "SK hynix Inc. American Depositary Shares • Robinhood Token",
            "0x84CAb63bc87912E71ad199ff14A0bA45de68FeF8",
        ),
        _robinhood_asset(
            "OTHER",
            "Other-chain token",
            "0x" + "33" * 20,
            chain_id=1,
        ),
    ]

    result = _load_robinhood_tokens_from_registry(
        downloader,
        [{"chain": "robinhoodchain", "chain_id": 4663}],
    )
    by_symbol = {token["shortcut"]: token for token in result}

    assert set(by_symbol) == {"AAPL", "SKHY"}
    assert by_symbol["AAPL"] == {
        "address": "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9",
        "chain": "robinhoodchain",
        "chain_id": 4663,
        "decimals": 18,
        "name": "Apple • Robinhood Token",
        "shortcut": "AAPL",
    }
    assert by_symbol["SKHY"]["address"] == (
        "0x84cab63bc87912e71ad199ff14a0ba45de68fef8"
    )
    assert by_symbol["SKHY"]["decimals"] == 18


def test_generated_robinhood_definitions_use_canonical_addresses():
    definitions = dl.load_json_file(dl.DEFINITIONS_PATH)
    network = next(
        network
        for network in definitions["networks"]
        if network["chain_id"] == 4663
    )
    tokens_by_address = {
        token["address"]: token
        for token in definitions["erc20_tokens"]
        if token["chain_id"] == 4663
    }
    expected = {
        "0x5fc5360d0400a0fd4f2af552add042d716f1d168": ("USDG", 6),
        "0xaf3d76f1834a1d425780943c99ea8a608f8a93f9": ("AAPL", 18),
        "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec": ("NVDA", 18),
        "0x322f0929c4625ed5bad873c95208d54e1c003b2d": ("TSLA", 18),
        "0x84cab63bc87912e71ad199ff14a0ba45de68fef8": ("SKHY", 18),
    }

    assert network["chain"] == "robinhoodchain"
    assert network["shortcut"] == "ETH"
    assert network["coingecko_network_id"] == "robinhood"
    for address, (shortcut, decimals) in expected.items():
        assert tokens_by_address[address]["shortcut"] == shortcut
        assert tokens_by_address[address]["decimals"] == decimals

    spoofed_skhy_address = "0x84cab63bc87912e71ad199ff14a0ba45de68fef9"
    assert spoofed_skhy_address not in tokens_by_address


def test_load_robinhood_tokens_skips_registry_without_network():
    downloader = mock.Mock()

    assert _load_robinhood_tokens_from_registry(downloader, networks) == []
    downloader.get_robinhood_assets.assert_not_called()


@pytest.mark.parametrize(
    "assets",
    (
        [],
        [
            {
                "tokenSymbol": "BAD",
                "tokenName": "Missing address",
                "deployments": [{"chainId": 4663}],
            }
        ],
        [
            _robinhood_asset(
                "BAD",
                "Invalid address",
                "0x1234",
            )
        ],
        [
            {
                "tokenSymbol": 123,
                "tokenName": "Invalid symbol type",
                "deployments": [
                    {"contractAddress": "0x" + "22" * 20, "chainId": 4663}
                ],
            }
        ],
    ),
)
def test_load_robinhood_tokens_rejects_incomplete_registry(assets):
    downloader = mock.Mock()
    downloader.get_robinhood_assets.return_value = assets

    with pytest.raises(ValueError):
        _load_robinhood_tokens_from_registry(
            downloader,
            [{"chain": "robinhoodchain", "chain_id": 4663}],
        )


def test_load_robinhood_tokens_rejects_conflicting_address():
    downloader = mock.Mock()
    address = "0x" + "44" * 20
    downloader.get_robinhood_assets.return_value = [
        _robinhood_asset("ONE", "First", address),
        _robinhood_asset("TWO", "Second", address),
    ]

    with pytest.raises(ValueError, match="Conflicting Robinhood registry entries"):
        _load_robinhood_tokens_from_registry(
            downloader,
            [{"chain": "robinhoodchain", "chain_id": 4663}],
        )


def test_download_checkpoints_cache_before_robinhood_failure(monkeypatch):
    downloader = mock.Mock()
    downloader.get_coingecko_asset_platforms.return_value = []
    downloader.get_defillama_chains.return_value = []
    events = []
    downloader.save_cache.side_effect = lambda: events.append("save")

    monkeypatch.setattr(dl, "Downloader", mock.Mock(return_value=downloader))
    monkeypatch.setattr(dl, "_load_ethereum_networks_from_repo", lambda: [])
    monkeypatch.setattr(dl, "_load_erc20_tokens_from_coingecko", lambda *_: [])
    monkeypatch.setattr(dl, "_load_erc20_tokens_from_repo", lambda *_: [])

    def fail_robinhood_load(*_):
        events.append("robinhood")
        raise ValueError("invalid Robinhood registry response")

    monkeypatch.setattr(
        dl, "_load_robinhood_tokens_from_registry", fail_robinhood_load
    )

    with pytest.raises(ValueError, match="invalid Robinhood registry response"):
        dl.download.callback(
            refresh=None,
            interactive=False,
            force_changes=False,
            show_all=False,
            show_added=False,
            check_builtin=False,
            verbose=False,
            sleep_duration=0,
            trace_address=None,
            no_onchain_decimals=False,
        )

    assert events == ["save", "robinhood"]


def test_merge_erc20_token_sources_uses_last_source_and_retains_other_tokens():
    address = "0x" + "55" * 20
    unrelated = {**erc20_tokens[0]}
    coingecko = {
        "address": address,
        "chain": "robinhoodchain",
        "chain_id": 4663,
        "decimals": 18,
        "name": "CoinGecko name",
        "shortcut": "OLD",
    }
    official = {
        **coingecko,
        "name": "Official name",
        "shortcut": "OFFICIAL",
    }

    result = _merge_erc20_token_sources(
        [unrelated],
        [coingecko],
        [official],
    )

    assert unrelated in result
    assert official in result
    assert coingecko not in result


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
    assert emitted == [gated]  # ungated provider does not feed output


def test_dedup_ignores_unknown_chains():
    emitted, conflicts = _dedup_display_formats(
        [("x.json", True, [_rec(chain_id=999)])], {1}
    )
    assert emitted == []
    assert conflicts == []


def test_write_display_formats_log_has_both_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DISPLAY_FORMATS_LOG_PATH", tmp_path / "out.log")
    _write_display_formats_log(
        unsupported=[("provA/f.json", "unsupported-formatter", "enum (field 'X')")],
        conflicts=[("chain=1 address=0xabc selector=0xdead", "provA/f.json", "provB/g.json")],
    )
    text = (tmp_path / "out.log").read_text()
    assert "unsupported features" in text
    assert "unsupported-formatter" in text
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
