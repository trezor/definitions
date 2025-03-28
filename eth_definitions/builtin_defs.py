from __future__ import annotations

import json
from pathlib import Path

import click

from .common import (
    DEFINITIONS_PATH,
    Network,
    ERC20Token,
    hash_dict_on_keys,
    load_json_file,
)

HERE = Path(__file__).parent
ROOT = HERE.parent
TREZOR_COMMON = ROOT / "coins_details" / "trezor_common"
ETH_DEFS_DIR = TREZOR_COMMON / "defs" / "ethereum"

EXCLUDES = ("deleted", "coingecko_id", "coingecko_rank")


@click.command()
@click.option(
    "-d",
    "--deffile",
    type=click.Path(resolve_path=True, dir_okay=False, path_type=Path),
    default=DEFINITIONS_PATH,
    help="File where the definitions will be saved in json format. If file already exists, it is used to check the changes in definitions.",
)
@click.option(
    "-t",
    "--top",
    type=int,
    default=50,
    help="Coingecko rank cutoff that should be built-in.",
)
def check_builtin(deffile: Path, top: int) -> None:
    """Comparing current definitions with built-in ones."""
    defs = load_json_file(deffile)
    check_ok = check_builtin_defs(defs["networks"], defs["tokens"], top)
    if check_ok:
        print("SUCCESS: validation passed.")
    else:
        raise click.ClickException("ERROR: validation failed. See content above.")


def check_builtin_defs(
    networks: list[Network], tokens: list[ERC20Token], top: int = 50
) -> bool:
    builtin_networks = _load_raw_builtin_ethereum_networks()
    builtin_tokens = _load_raw_builtin_erc20_tokens()

    hashes_builtin = {
        hash_dict_on_keys(b, EXCLUDES): b for b in builtin_networks + builtin_tokens
    }
    hashes = {hash_dict_on_keys(d, EXCLUDES) for d in networks + tokens}
    ids = {(d["chain_id"], d.get("address")): d for d in networks + tokens}

    checks_ok = True

    # check definition differences
    for builtin_hash, builtin_def in hashes_builtin.items():
        if builtin_hash not in hashes:
            checks_ok = False
            name = "TOKEN" if "address" in builtin_def else "NETWORK"
            print(f"== BUILT-IN {name} DEFINITION OUTDATED ==")
            print("BUILT-IN:")
            print(json.dumps(builtin_def, sort_keys=True, indent=None))
            print("CURRENT:")
            key = (builtin_def["chain_id"], builtin_def.get("address"))
            print(json.dumps(ids[key], sort_keys=True, indent=None))

    # check missing top 50 defs
    top_networks = [
        network for network in networks if network.get("coingecko_rank", top + 1) <= top
    ]
    top_chains = {network["chain"] for network in top_networks}
    top_tokens = [
        token for token in tokens if token.get("coingecko_rank", top + 1) <= top
    ]

    for network in top_networks:
        hash = hash_dict_on_keys(network, EXCLUDES)
        if hash not in hashes_builtin:
            checks_ok = False
            print("== MISSING BUILT-IN NETWORK DEFINITION ==")
            print(json.dumps(network, sort_keys=True, indent=None))

    missing_token_heading = False
    for token in top_tokens:
        if token["chain"] not in top_chains:
            continue
        hash = hash_dict_on_keys(token, EXCLUDES)
        if hash not in hashes_builtin:
            checks_ok = False
            if not missing_token_heading:
                print("== MISSING BUILT-IN TOKEN DEFINITIONS ==")
                missing_token_heading = True
            print(json.dumps(token, sort_keys=True, indent=None) + ",")

    return checks_ok


def _load_raw_builtin_ethereum_networks() -> list[Network]:
    """Load ethereum networks from `ethereum/networks.json`"""
    content = _get_eth_file_content("networks.json")
    return json.loads(content)


def _load_raw_builtin_erc20_tokens() -> list[ERC20Token]:
    """Load ERC20 tokens from `ethereum/tokens.json`."""
    content = _get_eth_file_content("tokens.json")
    tokens_data = json.loads(content)

    all_tokens: list[ERC20Token] = []

    for chain_id_and_chain, tokens in tokens_data.items():
        chain_id, chain = chain_id_and_chain.split(";", maxsplit=1)
        for token in tokens:
            token.update(
                chain=chain,
                chain_id=int(chain_id),
            )
            all_tokens.append(token)

    return all_tokens


def _get_eth_file_content(file: str) -> str:
    """Get the content of a file from `trezor-common`."""
    return (ETH_DEFS_DIR / file).read_text()
