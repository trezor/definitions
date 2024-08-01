#!/usr/bin/env python3
"""Fetch information about coins and tokens supported by Trezor and update it in coins_details.json."""

from __future__ import annotations

import json
import logging
import time
import typing as t
from dataclasses import asdict, dataclass, field
from pathlib import Path

import click
import trezor_common.tools.coin_info as coin_info
from trezor_common.tools.coin_info import Coin

if t.TYPE_CHECKING:
    from ..eth_definitions.common import Network, Token


class WalletInfo(t.TypedDict):
    name: str
    url: str


SupportEntry = t.Dict[str, bool]


@dataclass
class CoinDetail:
    id: str
    coingecko_id: str | None
    name: str
    shortcut: str
    support: SupportEntry
    networks: set[str | None]
    wallets: list[WalletInfo] = field(init=False)

    def __post_init__(self) -> None:
        # make sure to make a copy of the wallets list, it will be modified later
        wallets = WALLETS.get(self.id, [])
        self.wallets = wallets[:]

    @classmethod
    def from_coin(cls, coin: Coin, support_info: dict[str, SupportEntry]) -> CoinDetail:
        key = coin["key"]
        cg_id = COINGECKO_IDS.get(key)

        return cls(
            id=key,
            coingecko_id=cg_id,
            name=coin["name"],
            shortcut=coin["shortcut"],
            support=support_info[key],
            networks={cg_id},
        )

    @classmethod
    def from_eth_network(cls, network: Network) -> CoinDetail:
        cg_id = network.get("coingecko_id")
        key = f"eth:{network['shortcut']}:{network['chain_id']}"
        new = cls(
            id=key,
            coingecko_id=cg_id,
            name=network["name"],
            shortcut=network["shortcut"],
            support={model: True for model in MODELS},
            networks={cg_id},
        )
        new.wallets.extend(WALLETS_ETH_3RDPARTY)
        return new

    @classmethod
    def from_eth_token(cls, token: Token, network: Network) -> CoinDetail:
        cg_id = token.get("coingecko_id")
        network_cg_id = network.get("coingecko_id")

        key = f"erc20:{network['chain']}:{token['address']}"
        new = cls(
            id=key,
            coingecko_id=cg_id,
            name=token["name"],
            shortcut=token["shortcut"],
            support={model: True for model in MODELS},
            networks={network_cg_id},
        )
        network_key = f"eth:{network['shortcut']}:{network['chain_id']}"
        new.wallets.extend(WALLETS.get(network_key, []))
        new.wallets.extend(WALLETS_ETH_3RDPARTY)
        return new

    def merge(self, other: CoinDetail) -> None:
        assert self.coingecko_id == other.coingecko_id, "Cannot merge different coins"
        self.support = {
            model: self.support[model] or other.support[model] for model in MODELS
        }
        for wallet in other.wallets:
            if wallet not in self.wallets:
                self.wallets.append(wallet)
        self.networks.update(other.networks)

    def to_json(self) -> dict[str, t.Any]:
        d = asdict(self)
        d["networks"] = list(sorted(n for n in self.networks if n))
        return d


HERE = Path(__file__).parent
ROOT = HERE.parent
COINS_DETAILS_JSON = ROOT / "coins_details.json"
COINS_LIST = ROOT / "supported_coins_list.txt"

LOG = logging.getLogger(__name__)

WALLETS = coin_info.load_json(HERE / "wallets.json")
OVERRIDES = coin_info.load_json(HERE / "coins_details.override.json")
COINGECKO_IDS = coin_info.load_json(HERE / "coingecko_ids.json")
DEFINITIONS_LATEST = coin_info.load_json(ROOT / "definitions-latest.json")

# automatic wallet entries
WALLET_SUITE = WalletInfo(name="Trezor Suite", url="https://trezor.io/trezor-suite")
WALLETS_ETH_3RDPARTY = [
    WalletInfo(name="Metamask", url="https://metamask.io/"),
    WalletInfo(name="Rabby", url="https://rabby.io/"),
]

TREZOR_KNOWN_URLS = ("https://trezor.io/trezor-suite",)

MODELS = {"T1B1", "T2T1", "T2B1", "T3T1"}


def summary(coins: dict[str, t.Any]) -> dict[str, t.Any]:
    counter = {model: 0 for model in MODELS}
    for coin in coins.values():
        for model in counter:
            counter[model] += coin["support"].get(model, False)

    return dict(
        updated_at=int(time.time()),
        updated_at_readable=time.asctime(),
        support_counter=counter,
    )


def dict_merge(orig: t.Any, new: t.Any) -> t.Any:
    if isinstance(new, dict) and isinstance(orig, dict):
        for k, v in new.items():
            orig[k] = dict_merge(orig.get(k), v)
        return orig
    elif isinstance(new, list) and isinstance(orig, list):
        if new and new[0] == "...":
            return orig + new[1:]
        return new
    else:
        return new


def check_missing_data(cg_ids: dict[str | None, CoinDetail]) -> dict[str, CoinDetail]:
    res = {}
    for cg_id, coin in cg_ids.items():
        if cg_id is None:
            LOG.info("Skipping coins without coingecko_id")
            continue

        hide = False
        # check wallets
        for wallet in coin.wallets:
            name = wallet.get("name")
            url = wallet.get("url")
            if not name or not url:
                LOG.warning(f"{coin.coingecko_id}: Bad wallet entry")
                hide = True
                continue
            if "trezor" in name.lower() and url not in TREZOR_KNOWN_URLS:
                LOG.warning(f"{coin.coingecko_id}: Strange URL for Trezor Wallet")

        if not any(coin.support[model] for model in MODELS):
            LOG.info(f"{coin.coingecko_id}: Coin not enabled on either device")
            hide = True

        if not coin.wallets:
            LOG.debug(f"{coin.coingecko_id}: Missing wallet")

        if "Testnet" in coin.name or "Regtest" in coin.name:
            LOG.debug(f"{coin.coingecko_id}: Hiding testnet")
            hide = True

        if not hide:
            res[cg_id] = coin

    return res


def apply_overrides(coins: dict[str, t.Any]) -> None:
    for key, override in OVERRIDES.items():
        if key not in coins:
            LOG.warning(f"override without coin: {key}")
            continue

        dict_merge(coins[key], override)


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
    logging.basicConfig(level=log_level)

    coin_info_defs, _ = coin_info.coin_info_with_duplicates()
    support_info = {
        key: {model: bool(value.get(model)) for model in MODELS}
        for key, value in coin_info.support_info(coin_info_defs).items()
    }

    cg_ids_unfiltered: dict[str | None, CoinDetail] = {}

    # Update non-ETH things from coin_info
    for coin in coin_info_defs.bitcoin + coin_info_defs.misc:
        cdet = CoinDetail.from_coin(coin, support_info)
        cg_ids_unfiltered.setdefault(cdet.coingecko_id, cdet).merge(cdet)

    for coin in coin_info_defs.nem:
        cdet = CoinDetail.from_coin(coin, support_info)
        cdet.networks = {"nem"}
        cg_ids_unfiltered.setdefault(cdet.coingecko_id, cdet).merge(cdet)

    # Update ETH things from our own definitions
    eth_networks: list[Network] = [
        d for d in DEFINITIONS_LATEST["networks"] if not d.get("deleted")
    ]
    eth_tokens: list[Token] = [
        d for d in DEFINITIONS_LATEST["tokens"] if not d.get("deleted")
    ]

    for network in eth_networks:
        cdet = CoinDetail.from_eth_network(network)
        cg_ids_unfiltered.setdefault(cdet.coingecko_id, cdet).merge(cdet)

    chain_id_to_network = {net["chain_id"]: net for net in eth_networks}
    assert len(chain_id_to_network) == len(eth_networks), "Duplicate network keys"

    for token in eth_tokens:
        network = chain_id_to_network[token["chain_id"]]
        cdet = CoinDetail.from_eth_token(token, network)
        cg_ids_unfiltered.setdefault(cdet.coingecko_id, cdet).merge(cdet)

    cg_ids = check_missing_data(cg_ids_unfiltered)

    cg_json = {cg_id: coin.to_json() for cg_id, coin in cg_ids.items()}
    apply_overrides(cg_json)
    info = summary(cg_json)
    details = dict(coins=cg_json, info=info)

    print(json.dumps(info, sort_keys=True, indent=4))
    with open(COINS_DETAILS_JSON, "w") as f:
        json.dump(details, f, sort_keys=True, indent=1)
        f.write("\n")

    with open(COINS_LIST, "w") as f:
        f.write(f"Updated at: {info['updated_at_readable']}\n")
        for cg_id, coin in cg_json.items():
            f.write(f'{cg_id} {coin["name"]} ({coin["shortcut"]})\n')


if __name__ == "__main__":
    main()
