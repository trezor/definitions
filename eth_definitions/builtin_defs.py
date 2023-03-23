from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import click
import requests

from .common import DEFINITIONS_PATH, Network, Token, hash_dict_on_keys, load_json_file

if TYPE_CHECKING:
    from .common import DEFINITION_TYPE


GH_COMMON_ETH_LINK = (
    "https://raw.githubusercontent.com/trezor/trezor-common/master/defs/ethereum/"
)
GH_BACKUP_ETH_LINK = "https://raw.githubusercontent.com/trezor/trezor-firmware/marnova/ethereum_defs_from_host/common/defs/ethereum/"


@click.command()
@click.option(
    "-d",
    "--deffile",
    type=click.Path(resolve_path=True, dir_okay=False, path_type=Path),
    default=DEFINITIONS_PATH,
    help="File where the definitions will be saved in json format. If file already exists, it is used to check the changes in definitions.",
)
def check_builtin(deffile: Path) -> None:
    """Comparing current definitions with built-in ones."""
    check_ok = check_from_definition_file(deffile)
    if check_ok:
        print("SUCCESS: validation passed.")
    else:
        raise click.ClickException("ERROR: validation failed. See content above.")


def check_from_definition_file(file: Path = DEFINITIONS_PATH) -> bool:
    defs = load_json_file(file)
    networks = defs["networks"]
    tokens = defs["tokens"]
    return check_builtin_defs(networks, tokens)


def check_builtin_defs(networks: list[Network], tokens: list[Token]) -> bool:
    networks_ok = _check_networks(networks)
    tokens_ok = _check_tokens(tokens)
    return networks_ok and tokens_ok


def _check_networks(networks: list[Network]) -> bool:
    builtin_networks = _load_raw_builtin_ethereum_networks()
    return _check(networks, builtin_networks, "NETWORK")


def _check_tokens(tokens: list[Token]) -> bool:
    builtin_tokens = _load_raw_builtin_erc20_tokens()
    return _check(tokens, builtin_tokens, "TOKEN")


def _load_raw_builtin_ethereum_networks() -> list[Network]:
    """Load ethereum networks from `ethereum/networks.json`"""
    content = _get_eth_file_content("networks.json")
    return json.loads(content)


def _load_raw_builtin_erc20_tokens() -> list[Token]:
    """Load ERC20 tokens from `ethereum/tokens.json`."""
    content = _get_eth_file_content("tokens.json")
    tokens_data = json.loads(content)

    all_tokens: list[Token] = []

    for chain_id_and_chain, tokens in tokens_data.items():
        chain_id, chain = chain_id_and_chain.split(";", maxsplit=1)
        for token in tokens:
            token.update(
                chain=chain,
                chain_id=int(chain_id),
            )
            all_tokens.append(token)

    return all_tokens


def _check(
    defs: list["DEFINITION_TYPE"], builtin_defs: list["DEFINITION_TYPE"], name: str
) -> bool:
    check_ok = True
    EXCLUDES = ("deleted", "coingecko_id")

    hashes_builtin = {hash_dict_on_keys(b, EXCLUDES): b for b in builtin_defs}
    hashes = {hash_dict_on_keys(d, EXCLUDES) for d in defs}

    for builtin_hash, builtin_def in hashes_builtin.items():
        if builtin_hash not in hashes:
            check_ok = False
            print(f"== BUILT-IN {name} DEFINITION OUTDATED ==")
            print(json.dumps(builtin_def, sort_keys=True, indent=None))

    return check_ok


def _get_eth_file_content(file: str) -> str:
    """Get the content of a file from `trezor-common` or from branch in `firmware` repo."""
    try:
        # try to get the file in common repo - it will not be available unless we merge
        # the FW PR first
        response = requests.get(GH_COMMON_ETH_LINK + file)
        response.raise_for_status()
    except requests.exceptions.HTTPError:
        # if we fail, try to get the file from the FW repo
        response = requests.get(GH_BACKUP_ETH_LINK + file)
        response.raise_for_status()

    return response.text
