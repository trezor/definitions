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

import coin_info

if TYPE_CHECKING:
    from coin_info import (
        Coin,
        Coins,
        SupportData,
        SupportInfo,
        SupportInfoItem,
        WalletItems,
    )

LOG = logging.getLogger(__name__)

HERE = Path(__file__).parent
ROOT = HERE.parent
COINS_DETAILS_JSON = ROOT / "coins_details.json"

OPTIONAL_KEYS = ("links", "notes", "wallet")
ALLOWED_SUPPORT_STATUS = ("yes", "no")

WALLETS = coin_info.load_json("wallets.json")
OVERRIDES = coin_info.load_json(HERE / "coins_details.override.json")

# automatic wallet entries
WALLET_SUITE = {"Trezor Suite": "https://suite.trezor.io"}
WALLET_NEM = {"Nano Wallet": "https://nemplatform.com/wallets/#desktop"}
WALLETS_ETH_3RDPARTY = {
    "MyEtherWallet": "https://www.myetherwallet.com",
    "MyCrypto": "https://mycrypto.com",
}


TREZOR_KNOWN_URLS = (
    "https://suite.trezor.io",
    "https://wallet.trezor.io",
)


def summary(coins: dict[str, Coin]) -> dict[str, Any]:
    t1_coins = 0
    t2_coins = 0
    for coin in coins.values():
        if coin.get("hidden"):
            continue

        t1_enabled = coin["t1_enabled"] == "yes"
        t2_enabled = coin["t2_enabled"] == "yes"
        if t1_enabled:
            t1_coins += 1
        if t2_enabled:
            t2_coins += 1

    return dict(
        updated_at=int(time.time()),
        updated_at_readable=time.asctime(),
        t1_coins=t1_coins,
        t2_coins=t2_coins,
    )


def _is_supported(support: SupportData, trezor_version: str) -> str:
    # True or version string means YES
    # False or None means NO
    return "yes" if support.get(trezor_version) else "no"


def _suite_support(coin: Coin, support: SupportInfoItem) -> bool:
    """Check the "suite" support property.
    If set, check that at least one of the backends run on trezor.io.
    If yes, assume we support the coin in our wallet.
    Otherwise it's probably working with a custom backend, which means don't
    link to our wallet.c
    """
    if not support.get("suite"):
        return False
    return any(".trezor.io" in url for url in coin["blockbook"])


def dict_merge(orig: dict, new: dict) -> dict:
    if isinstance(new, dict) and isinstance(orig, dict):
        for k, v in new.items():
            orig[k] = dict_merge(orig.get(k), v)
        return orig
    else:
        return new


def update_simple(
    coins: Coins, support_info: SupportInfo, type: str
) -> dict[str, Coin]:
    res = {}
    for coin in coins:
        key = coin["key"]
        support = support_info[key]

        details = dict(
            name=coin["name"],
            shortcut=coin["shortcut"],
            type=type,
            t1_enabled=_is_supported(support, "trezor1"),
            t2_enabled=_is_supported(support, "trezor2"),
            wallet={},
        )
        for k in OPTIONAL_KEYS:
            if k in coin:
                details[k] = coin[k]

        details["wallet"].update(WALLETS.get(key, {}))

        res[key] = details

    return res


def update_bitcoin(coins: Coins, support_info: SupportInfo) -> dict[str, Coin]:
    res = update_simple(coins, support_info, "coin")
    for coin in coins:
        key: str = coin["key"]
        support = support_info[key]
        details = dict(
            name=coin["coin_label"],
            links=dict(Homepage=coin["website"], Github=coin["github"]),
            wallet=WALLET_SUITE if _suite_support(coin, support) else {},
        )
        dict_merge(res[key], details)

    return res


def update_erc20(
    coins: Coins, networks: Coins, support_info: SupportInfo
) -> dict[str, Coin]:
    # TODO skip disabled networks?
    network_support = {n["chain"]: support_info.get(n["key"]) for n in networks}
    network_testnets = {n["chain"] for n in networks if "Testnet" in n["name"]}
    res = update_simple(coins, support_info, "erc20")
    for coin in coins:
        key = coin["key"]
        chain = coin["chain"]

        hidden = False
        if chain in network_testnets:
            hidden = True
        if "deprecation" in coin:
            hidden = True

        if network_support.get(chain, {}).get("suite"):
            wallets = WALLET_SUITE
        else:
            wallets = WALLETS_ETH_3RDPARTY

        details = dict(
            network=chain,
            address=coin["address"],
            shortcut=coin["shortcut"],
            links={},
            wallet=wallets,
        )
        if hidden:
            details["hidden"] = True
        if coin.get("website"):
            details["links"]["Homepage"] = coin["website"]
        if coin.get("social", {}).get("github"):
            details["links"]["Github"] = coin["social"]["github"]

        dict_merge(res[key], details)

    return res


def update_ethereum_networks(
    coins: Coins, support_info: SupportInfo
) -> dict[str, Coin]:
    res = update_simple(coins, support_info, "coin")
    for coin in coins:
        key = coin["key"]
        if support_info[key].get("suite"):
            wallets = WALLET_SUITE
        else:
            wallets = WALLETS_ETH_3RDPARTY
        details = dict(links=dict(Homepage=coin.get("url")), wallet=wallets)
        dict_merge(res[key], details)

    return res


def update_nem_mosaics(coins: Coins, support_info: SupportInfo) -> dict[str, Coin]:
    res = update_simple(coins, support_info, "mosaic")
    for coin in coins:
        key = coin["key"]
        details = dict(wallet=WALLET_NEM)
        dict_merge(res[key], details)

    return res


def check_missing_data(coins: dict[str, Coin]) -> None:
    for k, coin in coins.items():
        hide = False

        if "Homepage" not in coin.get("links", {}):
            level = logging.WARNING
            if k.startswith("erc20:"):
                level = logging.INFO
            LOG.log(level, f"{k}: Missing homepage")
            hide = True
        if coin["t1_enabled"] not in ALLOWED_SUPPORT_STATUS:
            LOG.error(f"{k}: Unknown t1_enabled: {coin['t1_enabled']}")
            hide = True
        if coin["t2_enabled"] not in ALLOWED_SUPPORT_STATUS:
            LOG.error(f"{k}: Unknown t2_enabled: {coin['t2_enabled']}")
            hide = True

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

        if coin["t1_enabled"] == "no" and coin["t2_enabled"] == "no":
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

    # summary of hidden coins
    hidden_coins = [k for k, coin in coins.items() if coin.get("hidden")]
    for key in hidden_coins:
        del coins[key]


def apply_overrides(coins: dict[str, Coin]) -> None:
    for key, override in OVERRIDES.items():
        if key not in coins:
            LOG.warning(f"override without coin: {key}")
            continue

        dict_merge(coins[key], override)


def finalize_wallets(coins: dict[str, Coin]) -> None:
    def sort_key(w: WalletItems) -> tuple[int, str]:
        if "trezor.io" in w["url"]:
            return 0, w["name"]
        else:
            return 1, w["name"]

    for coin in coins.values():
        wallets_list = [
            dict(name=name, url=url) for name, url in coin["wallet"].items() if url
        ]
        wallets_list.sort(key=sort_key)
        coin["wallet"] = wallets_list


@click.command()
@click.option("-v", "--verbose", is_flag=True, help="Display more info")
def main(verbose: bool):
    # setup logging
    log_level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    root.addHandler(handler)

    defs, _ = coin_info.coin_info_with_duplicates()
    support_info = coin_info.support_info(defs)

    coins: dict[str, Coin] = {}
    coins.update(update_bitcoin(defs.bitcoin, support_info))
    coins.update(update_erc20(defs.erc20, defs.eth, support_info))
    coins.update(update_ethereum_networks(defs.eth, support_info))
    coins.update(update_nem_mosaics(defs.nem, support_info))
    coins.update(update_simple(defs.misc, support_info, "coin"))

    apply_overrides(coins)
    finalize_wallets(coins)

    check_missing_data(coins)

    info = summary(coins)
    details = dict(coins=coins, info=info)

    print(json.dumps(info, sort_keys=True, indent=4))
    with open(COINS_DETAILS_JSON, "w") as f:
        json.dump(details, f, sort_keys=True, indent=4)
        f.write("\n")


if __name__ == "__main__":
    main()
