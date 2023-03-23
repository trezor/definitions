from copy import deepcopy

from .download import _force_networks_fields_sizes_t1, _force_tokens_fields_sizes_t1
from .test_data import networks, tokens


def test_force_tokens_fields_sizes_t1_no_change():
    # No change
    all_tokens = deepcopy(tokens)
    _force_tokens_fields_sizes_t1(all_tokens)
    assert all_tokens == tokens


def test_force_tokens_fields_sizes_t1_value_error():
    # Invalid address - ValueError
    all_tokens = deepcopy(tokens)
    all_tokens[0]["address"] += "0"
    _force_tokens_fields_sizes_t1(all_tokens)
    # First token is missing
    assert len(all_tokens) == len(tokens) - 1
    assert all_tokens[:] == tokens[1:]


def test_force_tokens_fields_sizes_t1_longer_address():
    # Invalid address - longer than 20 bytes
    all_tokens = deepcopy(tokens)
    bad_index = 1
    all_tokens[bad_index]["address"] += "00"
    _force_tokens_fields_sizes_t1(all_tokens)
    # Bad index is missing, all others are the same
    assert len(all_tokens) == len(tokens) - 1
    assert all_tokens[:bad_index] == tokens[:bad_index]
    assert all_tokens[bad_index:] == tokens[bad_index + 1 :]


def test_force_tokens_fields_sizes_t1_two_invalid():
    # Invalid address - two are bad, checking correct deletion
    all_tokens = deepcopy(tokens)
    all_tokens[1]["address"] += "0"
    all_tokens[3]["address"] += "00"
    _force_tokens_fields_sizes_t1(all_tokens)
    # Two tokens are missing
    assert len(all_tokens) == len(tokens) - 2
    popped_tokens = deepcopy(tokens)
    popped_tokens.pop(3)
    popped_tokens.pop(1)
    assert all_tokens == popped_tokens


def test_force_tokens_fields_sizes_t1_name_over_limit():
    # Name over limit
    all_tokens = deepcopy(tokens)
    all_tokens[0]["name"] = "a" * 512
    _force_tokens_fields_sizes_t1(all_tokens)
    # Name shortened
    assert all_tokens[0]["name"] == "a" * 256
    assert len(all_tokens) == len(tokens)
    assert all_tokens[1:] == tokens[1:]


def test_force_tokens_fields_sizes_t1_shortcut_over_limit():
    # Shortcut over limit
    all_tokens = deepcopy(tokens)
    all_tokens[0]["shortcut"] = "b" * 512
    _force_tokens_fields_sizes_t1(all_tokens)
    # Shortcut shortened
    assert all_tokens[0]["shortcut"] == "b" * 256
    assert len(all_tokens) == len(tokens)
    assert all_tokens[1:] == tokens[1:]


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
