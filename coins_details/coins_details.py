#!/usr/bin/env python3
"""Fetch information about coins and tokens supported by Trezor and update it in coins_details.json."""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

import trezor_common.tools.coin_info as coin_info

if TYPE_CHECKING:
    from trezor_common.tools.coin_info import Coins, SupportInfo, WalletItems

HERE = Path(__file__).parent
ROOT = HERE.parent
COINS_DETAILS_JSON = ROOT / "coins_details.json"
DEFINITIONS_LATEST_JSON = ROOT / "definitions-latest.json"
SUITE_SUPPORT_JSON = ROOT / "suite-support.json"
COINS_LIST = ROOT / "supported_coins_list.txt"

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.DEBUG)
file_handler = logging.FileHandler(HERE / "logs.log")
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)
LOG.addHandler(file_handler)

OPTIONAL_KEYS = ("links", "notes", "wallet")
ALLOWED_SUPPORT_STATUS = ("yes", "no")

WALLETS = coin_info.load_json("wallets.json")
OVERRIDES = coin_info.load_json(HERE / "coins_details.override.json")
DEFINITIONS_LATEST = coin_info.load_json(DEFINITIONS_LATEST_JSON)
SUITE_SUPPORT = coin_info.load_json(SUITE_SUPPORT_JSON)

# automatic wallet entries
WALLET_SUITE = {"Trezor Suite": "https://trezor.io/trezor-suite"}
WALLET_NEM = {"Nano Wallet": "https://nemplatform.com/wallets/#desktop"}
WALLETS_ETH_3RDPARTY = {
    "MyCrypto": "https://mycrypto.com",
    "Metamask": "https://metamask.io/",
    "Rabby": "https://rabby.io/",
}


TREZOR_KNOWN_URLS = (
    "https://suite.trezor.io",
    "https://wallet.trezor.io",
    "https://trezor.io/trezor-suite",
)

MODELS = {"T1B1", "T2T1", "T2B1"}


def summary(coins: Coins) -> dict[str, Any]:
    counter = {model: 0 for model in MODELS}
    for coin in coins:
        if coin.get("hidden"):
            continue

        for model in counter:
            counter[model] += coin["support"].get(model, 0)

    return dict(
        updated_at=int(time.time()),
        updated_at_readable=time.asctime(),
        support_counter=counter,
    )


def dict_merge(orig: Any, new: Any) -> dict:
    if isinstance(new, dict) and isinstance(orig, dict):
        for k, v in new.items():
            orig[k] = dict_merge(orig.get(k), v)
        return orig
    else:
        return new


def update_simple(coins: Coins, support_info: SupportInfo, type: str) -> Coins:
    res = []
    for coin in coins:
        key = coin["key"]
        support = {model: bool(support_info[key].get(model)) for model in MODELS}

        details = dict(
            key=key,
            name=coin["name"],
            shortcut=coin["shortcut"],
            type=type,
            support=support,
            wallet={},
        )
        for k in OPTIONAL_KEYS:
            if k in coin:
                details[k] = coin[k]

        details["wallet"].update(WALLETS.get(key, {}))

        res.append(details)

    return res


def update_bitcoin(coins: Coins, support_info: SupportInfo) -> Coins:
    res = update_simple(coins, support_info, "coin")
    for coin, updated in zip(coins, res):
        key: str = coin["key"]
        details = dict(
            name=coin["coin_label"],
            links=dict(Homepage=coin["website"], Github=coin["github"]),
            wallet=WALLET_SUITE if key in SUITE_SUPPORT else {},
        )
        dict_merge(updated, details)

    return res


def update_nem_mosaics(coins: Coins, support_info: SupportInfo) -> Coins:
    res = update_simple(coins, support_info, "mosaic")
    for coin in res:
        details = dict(wallet=WALLET_NEM)
        dict_merge(coin, details)

    return res


def check_missing_data(coins: Coins) -> Coins:
    for coin in coins:
        hide = False
        k = coin["key"]

        # check wallets
        for wallet in coin["wallet"]:
            name = wallet.get("name")
            url = wallet.get("url")
            if not name or not url:
                LOG.warning(f"{k}: Bad wallet entry")
                hide = True
                continue
            if "trezor" in name.lower() and url not in TREZOR_KNOWN_URLS:
                LOG.warning(f"{k}: Strange URL for Trezor Wallet")

        if not any(coin["support"][model] for model in MODELS):
            LOG.info(f"{k}: Coin not enabled on either device")
            hide = True

        if len(coin.get("wallet", [])) == 0:
            LOG.debug(f"{k}: Missing wallet")

        if "Testnet" in coin["name"] or "Regtest" in coin["name"]:
            LOG.debug(f"{k}: Hiding testnet")
            hide = True

        if not hide and coin.get("hidden"):
            LOG.info(f"{k}: Details are OK, but coin is still hidden")

        if hide:
            coin["hidden"] = 1

    return [coin for coin in coins if not coin.get("hidden")]


def apply_overrides(coins: Coins) -> None:
    for key, override in OVERRIDES.items():
        for coin in coins:
            if coin["key"] == key:
                dict_merge(coin, override)
                break
        else:
            LOG.warning(f"override without coin: {key}")


def finalize_wallets(coins: Coins) -> None:
    def sort_key(w: WalletItems) -> tuple[int, str]:
        if "trezor.io" in w["url"]:
            return 0, w["name"]
        else:
            return 1, w["name"]

    for coin in coins:
        wallets_list = [
            dict(name=name, url=url) for name, url in coin["wallet"].items() if url
        ]
        wallets_list.sort(key=sort_key)
        coin["wallet"] = wallets_list


@click.command()
@click.option("-v", "--verbose", count=True, help="Display more info")
def main(verbose: int):
    # setup logging
    if verbose == 0:
        log_level = logging.WARNING
    elif verbose == 1:
        log_level = logging.INFO
    else:
        log_level = logging.DEBUG
    root = logging.getLogger()
    root.setLevel(log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    root.addHandler(handler)

    coin_info_defs, _ = coin_info.coin_info_with_duplicates()
    support_info = coin_info.support_info(coin_info_defs)

    # Update non-ETH things from coin_info
    coins = (
        update_bitcoin(coin_info_defs.bitcoin, support_info)
        + update_nem_mosaics(coin_info_defs.nem, support_info)
        + update_simple(coin_info_defs.misc, support_info, "coin")
    )

    # Update ETH things from our own definitions
    eth_networks: Coins = DEFINITIONS_LATEST["networks"]
    eth_tokens: Coins = DEFINITIONS_LATEST["tokens"]
    # TODO: remove all testnet networks?
    for coin in eth_networks + eth_tokens:
        coin["wallet"] = WALLETS_ETH_3RDPARTY.copy()
        coin["support"] = {model: True for model in MODELS}

    chain_id_to_network = {net["chain_id"]: net for net in eth_networks}
    assert len(chain_id_to_network) == len(eth_networks), "Duplicate network keys"

    # Put key into network data
    for network in eth_networks:
        key = network["key"] = f"eth:{network['shortcut']}:{network['chain_id']}"
        if key in SUITE_SUPPORT:
            network["wallet"].update(WALLET_SUITE)

    # Put network name/key into token data
    for token in eth_tokens:
        network = chain_id_to_network[token["chain_id"]]
        token["key"] = f"erc20:{network['chain']}:{token['address']}"
        token["network"] = {
            "key": network["key"],
            "name": network["name"],
        }

        if network["key"] in SUITE_SUPPORT:
            token["wallet"].update(WALLET_SUITE)

    coins.extend(eth_networks)
    coins.extend(eth_tokens)
    coins.sort(key=lambda x: x["key"])

    apply_overrides(coins)
    finalize_wallets(coins)

    coins = check_missing_data(coins)

    # Coins should only keep these keys, delete all others
    keys_to_keep = (
        "id",
        "name",
        "shortcut",
        "support",
        "wallet",
        "coingecko_id",
        "network",
    )
    for coin in coins:
        # we want to use "key" for processing above, but "id" for output
        coin["id"] = coin["key"]
        for key in list(coin.keys()):
            if key not in keys_to_keep:
                del coin[key]

    # Adding `coingecko_id: null` for those not having `coingecko_id` key
    # Same for `network`
    for coin in coins:
        if "coingecko_id" not in coin:
            coin["coingecko_id"] = None
        if "network" not in coin:
            coin["network"] = None

    info = summary(coins)
    details = dict(coins=coins, info=info)

    print(json.dumps(info, sort_keys=True, indent=4))
    with open(COINS_DETAILS_JSON, "w") as f:
        json.dump(details, f, sort_keys=True, indent=1)
        f.write("\n")

    with open(COINS_LIST, "w") as f:
        f.write(f"Updated at: {info['updated_at_readable']}\n")
        for coin in coins:
            f.write(f'{coin["id"]} {coin["name"]} ({coin["shortcut"]})\n')


if __name__ == "__main__":
    main()
