#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict, defaultdict
from pathlib import Path
from typing import Dict  # for python38 support, must be used in type aliases
from typing import List  # for python38 support, must be used in type aliases
from typing import Any, Iterable, cast

from typing_extensions import (  # for python37 support, is not present in typing there
    Literal,
    TypedDict,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

DEFS_DIR = ROOT / "trezor_common" / "defs"
ETH_DEFS_DIR = ROOT / "ethereum-lists"


class SupportItemBool(TypedDict):
    supported: dict[str, bool]
    unsupported: dict[str, bool]


class SupportItemVersion(TypedDict):
    supported: dict[str, str]
    unsupported: dict[str, str]


class SupportData(TypedDict):
    connect: SupportItemBool
    suite: SupportItemBool
    trezor1: SupportItemVersion
    trezor2: SupportItemVersion


class SupportInfoItem(TypedDict):
    connect: bool
    suite: bool
    trezor1: Literal[False] | str
    trezor2: Literal[False] | str


SupportInfo = Dict[str, SupportInfoItem]

WalletItems = Dict[str, str]
WalletInfo = Dict[str, WalletItems]


class Coin(TypedDict):
    # Necessary fields for BTC - from BTC_CHECKS
    coin_name: str
    coin_shortcut: str
    coin_label: str
    website: str
    github: str
    maintainer: str
    curve_name: str
    address_type: int
    address_type_p2sh: int
    maxfee_kb: int
    minfee_kb: int
    hash_genesis_block: str
    xprv_magic: int
    xpub_magic: int
    xpub_magic_segwit_p2sh: int
    xpub_magic_segwit_native: int
    slip44: int
    segwit: bool
    decred: bool
    fork_id: int
    force_bip143: bool
    default_fee_b: dict[str, int]
    dust_limit: int
    blocktime_seconds: int
    signed_message_header: str
    uri_prefix: str
    min_address_length: int
    max_address_length: int
    bech32_prefix: str
    cashaddr_prefix: str

    # Other fields optionally coming from JSON
    links: dict[str, str]
    wallet: WalletItems
    curve: str
    decimals: int

    # Mandatory fields added later in coin.update()
    name: str
    shortcut: str
    key: str
    icon: str

    # Special ETH fields
    chain: str
    chain_id: str
    rskip60: bool
    url: str

    # Special erc20 fields
    symbol: str
    address: str
    address_bytes: bytes
    dup_key_nontoken: bool
    deprecation: dict[str, str]

    # Special NEM fields
    ticker: str

    # Fields that are being created
    unsupported: bool
    duplicate: bool
    support: SupportInfoItem

    # Backend-oriented fields
    blockchain_link: dict[str, Any]
    blockbook: list[str]
    bitcore: list[str]

    # Support fields
    t1_enabled: str
    t2_enabled: str


Coins = List[Coin]
CoinBuckets = Dict[str, Coins]


def load_json(*path: str | Path) -> Any:
    """Convenience function to load a JSON file from DEFS_DIR."""
    if len(path) == 1 and isinstance(path[0], Path):
        file = path[0]
    else:
        file = Path(DEFS_DIR, *path)

    return json.loads(file.read_text(), object_pairs_hook=OrderedDict)


# ====== CoinsInfo ======


class CoinsInfo(Dict[str, Coins]):
    """Collection of information about all known kinds of coins.

    It contains the following lists:
    `bitcoin` for btc-like coins,
    `eth` for ethereum networks,
    `erc20` for ERC20 tokens,
    `nem` for NEM mosaics,
    `misc` for other networks.

    Accessible as a dict or by attribute: `info["misc"] == info.misc`
    """

    def as_list(self) -> Coins:
        return sum(self.values(), [])

    def __getattr__(self, attr: str) -> Coins:
        if attr in self:
            return self[attr]
        else:
            raise AttributeError(attr)


# ======= Coin json loaders =======


def _load_btc_coins() -> Coins:
    """Load btc-like coins from `bitcoin/*.json`"""
    coins: Coins = []
    for file in DEFS_DIR.glob("bitcoin/*.json"):
        coin: Coin = load_json(file)
        coin.update(
            name=coin["coin_label"],  # type: ignore
            shortcut=coin["coin_shortcut"],
            key=f"bitcoin:{coin['coin_shortcut']}",
        )
        coins.append(coin)

    return coins


def _load_ethereum_networks() -> Coins:
    """Load ethereum networks from `ethereum/networks.json`"""
    chains_path = ETH_DEFS_DIR / "chains" / "_data" / "chains"
    networks: Coins = []
    for chain in sorted(
        chains_path.glob("eip155-*.json"),
        key=lambda x: int(x.stem.replace("eip155-", "")),
    ):
        chain_data = load_json(chain)
        shortcut = chain_data["nativeCurrency"]["symbol"]
        name = chain_data["name"]
        title = chain_data.get("title", "")
        is_testnet = "testnet" in name.lower() or "testnet" in title.lower()
        if is_testnet:
            slip44 = 1
        else:
            slip44 = chain_data.get("slip44", 60)

        if is_testnet and not shortcut.lower().startswith("t"):
            shortcut = "t" + shortcut

        rskip60 = shortcut in ("RBTC", "TRBTC")

        # strip out bullcrap in network naming
        if "mainnet" in name.lower():
            name = re.sub(r" mainnet.*$", "", name, flags=re.IGNORECASE)

        network = dict(
            chain=chain_data["shortName"],
            chain_id=chain_data["chainId"],
            slip44=slip44,
            shortcut=shortcut,
            name=name,
            rskip60=rskip60,
            url=chain_data["infoURL"],
            key=f"eth:{shortcut}",
        )
        networks.append(cast(Coin, network))

    return networks


def _load_erc20_tokens() -> Coins:
    """Load ERC20 tokens from `ethereum/tokens` submodule."""
    networks = _load_ethereum_networks()
    tokens: Coins = []
    for network in networks:
        chain = network["chain"]

        chain_path = ETH_DEFS_DIR / "tokens" / "tokens" / chain
        for file in sorted(chain_path.glob("*.json")):
            token: Coin = load_json(file)
            token.update(
                chain=chain,  # type: ignore
                chain_id=network["chain_id"],
                address_bytes=bytes.fromhex(token["address"][2:]),
                shortcut=token["symbol"],
                key=f"erc20:{chain}:{token['symbol']}",
            )
            tokens.append(token)

    return tokens


def _load_nem_mosaics() -> Coins:
    """Loads NEM mosaics from `nem/nem_mosaics.json`"""
    mosaics: Coins = load_json("nem/nem_mosaics.json")
    for mosaic in mosaics:
        shortcut = mosaic["ticker"].strip()
        mosaic.update(shortcut=shortcut, key=f"nem:{shortcut}")  # type: ignore
    return mosaics


def _load_misc() -> Coins:
    """Loads miscellaneous networks from `misc/misc.json`"""
    others: Coins = load_json("misc/misc.json")
    for other in others:
        other.update(key=f"misc:{other['shortcut']}")  # type: ignore
    return others


# ====== support info ======

MISSING_SUPPORT_MEANS_NO = ("connect", "suite")


def get_support_data() -> SupportData:
    """Get raw support data from `support.json`."""
    return load_json("support.json")


def is_token(coin: Coin) -> bool:
    return coin["key"].startswith("erc20:")


def support_info_single(support_data: SupportData, coin: Coin) -> SupportInfoItem:
    """Extract a support dict from `support.json` data.

    Returns a dict of support values for each "device", i.e., `support.json`
    top-level key.

    The support value for each device is determined in order of priority:
    * if the coin has an entry in `unsupported`, its support is `False`
    * if the coin has an entry in `supported` its support is that entry
      (usually a version string, or `True` for connect/suite)
    * if the coin doesn't have an entry, its support status is `None`
    """
    support_info_item = {}
    key = coin["key"]
    for device, values in support_data.items():
        assert isinstance(values, dict)
        if key in values["unsupported"]:
            support_value: Any = False
        elif key in values["supported"]:
            support_value = values["supported"][key]
        elif device in MISSING_SUPPORT_MEANS_NO:
            support_value = False
        else:
            support_value = None
        support_info_item[device] = support_value
    return cast(SupportInfoItem, support_info_item)


def support_info(coins: Iterable[Coin] | CoinsInfo | dict[str, Coin]) -> SupportInfo:
    """Generate Trezor support information.

    Takes a collection of coins and generates a support-info entry for each.
    The support-info is a dict with keys based on `support.json` keys.
    These are usually: "trezor1", "trezor2", "connect" and "suite".

    The `coins` argument can be a `CoinsInfo` object, a list or a dict of
    coin items.

    Support information is taken from `support.json`.
    """
    if isinstance(coins, CoinsInfo):
        coins = coins.as_list()
    elif isinstance(coins, dict):
        coins = coins.values()

    support_data = get_support_data()
    support: SupportInfo = {}
    for coin in coins:
        support[coin["key"]] = support_info_single(support_data, coin)

    return support


# ====== wallet info ======

WALLET_SUITE = {"Trezor Suite": "https://suite.trezor.io"}
WALLET_NEM = {"Nano Wallet": "https://nemplatform.com/wallets/#desktop"}
WALLETS_ETH_3RDPARTY = {
    "MyEtherWallet": "https://www.myetherwallet.com",
    "MyCrypto": "https://mycrypto.com",
}


# ====== data cleanup functions ======


def _ensure_mandatory_values(coins: Coins) -> None:
    """Checks that every coin has the mandatory fields: name, shortcut, key"""
    for coin in coins:
        if not all(coin.get(k) for k in ("name", "shortcut", "key")):
            raise ValueError(coin)


def symbol_from_shortcut(shortcut: str) -> tuple[str, str]:
    symsplit = shortcut.split(" ", maxsplit=1)
    return symsplit[0], symsplit[1] if len(symsplit) > 1 else ""


def mark_duplicate_shortcuts(coins: Coins) -> CoinBuckets:
    """Finds coins with identical symbols and sets their `duplicate` field.

    "Symbol" here means the first part of `shortcut` (separated by space),
    so, e.g., "BTL (Battle)" and "BTL (Bitlle)" have the same symbol "BTL".

    The result of this function is a dictionary of _buckets_, each of which is
    indexed by the duplicated symbol, or `_override`. The `_override` bucket will
    contain all coins that are set to `true` in `duplicity_overrides.json`.

    Each coin in every bucket will have its "duplicate" property set to True, unless
    it's explicitly marked as `false` in `duplicity_overrides.json`.
    """
    dup_symbols: CoinBuckets = defaultdict(list)

    for coin in coins:
        symbol, _ = symbol_from_shortcut(coin["shortcut"].lower())
        dup_symbols[symbol].append(coin)

    dup_symbols = {k: v for k, v in dup_symbols.items() if len(v) > 1}
    # mark duplicate symbols
    for values in dup_symbols.values():
        for coin in values:
            coin["duplicate"] = True

    return dup_symbols


def apply_duplicity_overrides(coins: Coins) -> Coins:
    overrides = load_json("duplicity_overrides.json")
    override_bucket: Coins = []
    for coin in coins:
        override_value = overrides.get(coin["key"])
        if override_value is True:
            override_bucket.append(coin)
        if override_value is not None:
            coin["duplicate"] = override_value

    return override_bucket


def deduplicate_erc20(buckets: CoinBuckets, networks: Coins) -> None:
    """Apply further processing to ERC20 duplicate buckets.

    This function works on results of `mark_duplicate_shortcuts`.

    Buckets that contain at least one non-token are ignored - symbol collisions
    with non-tokens always apply.

    Otherwise the following rules are applied:

    1. If _all tokens_ in the bucket have shortcuts with distinct suffixes, e.g.,
    `CAT (BitClave)` and `CAT (Blockcat)`, the bucket is cleared - all are considered
    non-duplicate.

    (If even one token in the bucket _does not_ have a distinct suffix, e.g.,
    `MIT` and `MIT (Mychatcoin)`, this rule does not apply and ALL tokens in the bucket
    are still considered duplicate.)

    2. If there is only one "main" token in the bucket, the bucket is cleared.
    That means that all other tokens must either be on testnets, or they must be marked
    as deprecated, with a deprecation pointing to the "main" token.
    """

    testnet_networks = {n["chain"] for n in networks if n["slip44"] == 1}

    def clear_bucket(bucket: Coins) -> None:
        # allow all coins, except those that are explicitly marked through overrides
        for coin in bucket:
            coin["duplicate"] = False

    for bucket in buckets.values():
        # Only check buckets that contain purely ERC20 tokens. Collision with
        # a non-token is always forbidden.
        if not all(is_token(c) for c in bucket):
            continue

        splits = (symbol_from_shortcut(coin["shortcut"]) for coin in bucket)
        suffixes = {suffix for _, suffix in splits}
        # if 1. all suffixes are distinct and 2. none of them are empty
        if len(suffixes) == len(bucket) and all(suffixes):
            clear_bucket(bucket)
            continue

        # protected categories:
        testnets = [coin for coin in bucket if coin["chain"] in testnet_networks]
        deprecated_by_same = [
            coin
            for coin in bucket
            if "deprecation" in coin
            and any(
                other["address"] == coin["deprecation"]["new_address"]
                for other in bucket
            )
        ]
        remaining = [
            coin
            for coin in bucket
            if coin not in testnets and coin not in deprecated_by_same
        ]
        if len(remaining) <= 1:
            for coin in deprecated_by_same:
                deprecated_symbol = "[deprecated] " + coin["symbol"]
                coin["shortcut"] = coin["symbol"] = deprecated_symbol
                coin["key"] += ":deprecated"
            clear_bucket(bucket)


def deduplicate_keys(all_coins: Coins) -> None:
    dups: CoinBuckets = defaultdict(list)
    for coin in all_coins:
        dups[coin["key"]].append(coin)

    for coins in dups.values():
        if len(coins) <= 1:
            continue
        for i, coin in enumerate(coins):
            if is_token(coin):
                coin["key"] += ":" + coin["address"][2:6].lower()  # first 4 hex chars
            elif "chain_id" in coin:
                coin["key"] += ":" + str(coin["chain_id"])
            else:
                coin["key"] += f":{i}"
                coin["dup_key_nontoken"] = True


def fill_blockchain_links(all_coins: CoinsInfo) -> None:
    blockchain_links = load_json("blockchain_link.json")
    for coins in all_coins.values():
        for coin in coins:
            link = blockchain_links.get(coin["key"])
            coin["blockchain_link"] = link
            if link and link["type"] == "blockbook":
                coin["blockbook"] = link["url"]
            else:
                coin["blockbook"] = []


def _btc_sort_key(coin: Coin) -> str:
    if coin["name"] in ("Bitcoin", "Testnet", "Regtest"):
        return "000000" + coin["name"]
    else:
        return coin["name"]


def collect_coin_info() -> CoinsInfo:
    """Returns all definition as dict organized by coin type.
    `coins` for btc-like coins,
    `eth` for ethereum networks,
    `erc20` for ERC20 tokens,
    `nem` for NEM mosaics,
    `misc` for other networks.
    """
    all_coins = CoinsInfo(
        bitcoin=_load_btc_coins(),
        eth=_load_ethereum_networks(),
        erc20=_load_erc20_tokens(),
        nem=_load_nem_mosaics(),
        misc=_load_misc(),
    )

    for coins in all_coins.values():
        _ensure_mandatory_values(coins)

    fill_blockchain_links(all_coins)

    return all_coins


def sort_coin_infos(all_coins: CoinsInfo) -> None:
    for k, coins in all_coins.items():
        if k == "bitcoin":
            coins.sort(key=_btc_sort_key)
        elif k == "nem":
            # do not sort nem
            pass
        elif k == "eth":
            # sort ethereum networks by chain_id
            coins.sort(key=lambda c: c["chain_id"])
        else:
            coins.sort(key=lambda c: c["key"].upper())


def coin_info_with_duplicates() -> tuple[CoinsInfo, CoinBuckets]:
    """Collects coin info, detects duplicates but does not remove them.

    Returns the CoinsInfo object and duplicate buckets.
    """
    all_coins = collect_coin_info()
    coin_list = all_coins.as_list()
    # generate duplicity buckets based on shortcuts
    buckets = mark_duplicate_shortcuts(all_coins.as_list())
    # apply further processing to ERC20 tokens, generate deprecations etc.
    deduplicate_erc20(buckets, all_coins.eth)
    # ensure the whole list has unique keys (taking into account changes from deduplicate_erc20)
    deduplicate_keys(coin_list)
    # apply duplicity overrides
    buckets["_override"] = apply_duplicity_overrides(coin_list)
    sort_coin_infos(all_coins)

    return all_coins, buckets


def coin_info() -> CoinsInfo:
    """Collects coin info, fills out support info and returns the result.

    Does not auto-delete duplicates. This should now be based on support info.
    """
    all_coins, _ = coin_info_with_duplicates()
    # all_coins["erc20"] = [
    #     coin for coin in all_coins["erc20"] if not coin.get("duplicate")
    # ]
    return all_coins
