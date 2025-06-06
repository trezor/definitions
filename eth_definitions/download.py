#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import click
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from trezorlib import tools

from .builtin_defs import check_builtin_defs
from .check_definitions import check_definitions_list
from .common import (
    DEFINITIONS_PATH,
    ChangeResolutionStrategy,
    Network,
    ERC20Token,
    SolanaToken,
    DefinitionsData,
    load_json_file,
    make_metadata,
    setup_logging,
    store_definitions_data,
)

HERE = Path(__file__).parent
ROOT_DIR = HERE.parent
ETHEREUM_LISTS = ROOT_DIR / "ethereum-lists"

TESTNET_WORDS = ("testnet", "devnet")

CACHE_PATH = HERE / "definitions-cache.json"

NETWORKS_PATH = ETHEREUM_LISTS / "chains" / "_data" / "chains"
TOKENS_PATH = ETHEREUM_LISTS / "tokens" / "tokens"


class CacheableError(Exception):
    def __init__(self, error_code: int):
        self.error_code = error_code


# ====== utils ======


class CachedDict(dict[str, Any]):
    """Generic cache object that caches to json."""

    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self.dirty = False
        if not self.cache_file.exists():
            self.cache_file.write_text(r"{}\n")
        self.load()

    def is_valid(self) -> bool:
        return not self._is_empty() and not self._is_expired()

    def _is_empty(self) -> bool:
        return len(self) == 0

    def _is_expired(self) -> bool:
        mtime = self.cache_file.stat().st_mtime if self.cache_file.exists() else 0
        time_diff = time.time() - mtime
        return time_diff > 3600

    def load(self) -> None:
        self.clear()
        self.update(json.loads(self.cache_file.read_text()))

    def save(self, force: bool = False) -> None:
        if not self.dirty and not force:
            return
        jsontext = json.dumps(self, sort_keys=True, indent=1)
        self.cache_file.write_text(jsontext + "\n")
        self.dirty = False

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        self.dirty = True


class Downloader:
    """Class that handles all the downloading and caching of Ethereum definitions."""

    def __init__(self, refresh: bool | None = None) -> None:
        """
        Args:
            refresh: If True, force refresh of data. If False, use cached data. If None,
            use cached data if available, otherwise force refresh.
        """
        self.cache = CachedDict(CACHE_PATH)
        self.refresh = refresh
        if refresh is None and not self.cache.is_valid():
            self.refresh = True
        self._init_requests_session()

    def save_cache(self):
        self.cache.save()

    def _download_json(self, url: str, **url_params: Any) -> Any:
        params = None
        encoded_params = None
        key = url

        # convert params to lower-case strings (especially for boolean values
        # because for CoinGecko API "True" != "true")
        if url_params:
            params = {key: str(value).lower() for key, value in url_params.items()}
            encoded_params = urlencode(sorted(params.items()))
            key += "?" + encoded_params

        if self.refresh is False and key not in self.cache:
            # refresh was explicitly disabled and key not found in cache
            raise ValueError(f"Key {key} not found in cache")

        if self.refresh is not True:
            # refresh was not explicitly enabled, so use cached data if available
            cached_result = self.cache.get(key)
            if cached_result is not None:
                if isinstance(cached_result, dict) and "error" in cached_result:
                    raise CacheableError(cached_result["error"])
                return cached_result

        logging.info(f"Fetching data from {url}")

        r = self.session.get(url, params=encoded_params, timeout=60)
        if r.status_code == requests.codes.forbidden:
            self.cache[key] = {"error": r.status_code}
            raise CacheableError(r.status_code)
        r.raise_for_status()
        data = r.json()
        self.cache[key] = data
        return data

    def _init_requests_session(self) -> None:
        self.session = requests.Session()
        # As CoinGecko API will block us after ~30 requests for the whole minute,
        # we need a way to retry the request multiple times.
        retries = Retry(total=5, status_forcelist=[502, 503, 504])
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def get_coingecko_asset_platforms(self) -> Any:
        url = "https://api.coingecko.com/api/v3/asset_platforms"
        return self._download_json(url)

    def get_defillama_chains(self) -> Any:
        url = "https://api.llama.fi/chains"
        return self._download_json(url)

    def get_coingecko_tokens_for_network(self, coingecko_network_id: str) -> list[Any]:
        url = f"https://tokens.coingecko.com/{coingecko_network_id}/all.json"
        try:
            data = self._download_json(url)
            return data.get("tokens", [])
        except CacheableError as err:
            # "Forbidden" is raised by Coingecko if no tokens are available under specified id
            pass
        except requests.exceptions.HTTPError as err:
            raise err

        return []

    def get_coingecko_coins_list(self) -> Any:
        url = "https://api.coingecko.com/api/v3/coins/list"
        return self._download_json(url, include_platform=True)

    def get_coingecko_top100(self) -> Any:
        url = "https://api.coingecko.com/api/v3/coins/markets"
        return self._download_json(
            url,
            vs_currency="usd",
            order="market_cap_desc",
            per_page=100,
            page=1,
            sparkline=False,
        )


def _get_testnet_status(*strings: str) -> bool:
    for s in strings:
        for testnet in TESTNET_WORDS:
            if testnet in s.lower():
                return True

    return False


def _load_ethereum_networks_from_repo() -> list[Network]:
    """Load ethereum networks from submodule."""
    networks: list[Network] = []
    for chain in sorted(
        NETWORKS_PATH.glob("eip155-*.json"),
        key=lambda x: int(x.stem.replace("eip155-", "")),
    ):
        chain_data = load_json_file(chain)
        shortcut = chain_data["nativeCurrency"]["symbol"]
        name = chain_data["name"]
        title = chain_data.get("title", "")
        is_testnet = _get_testnet_status(name, title)
        if is_testnet:
            slip44 = 1
        else:
            slip44 = chain_data.get("slip44", 60)

        if is_testnet and not shortcut.lower().startswith("t"):
            shortcut = "t" + shortcut

        # strip out bullcrap in network naming
        if "mainnet" in name.lower():
            name = re.sub(r" mainnet.*$", "", name, flags=re.IGNORECASE)

        coin = Network(
            chain=chain_data["shortName"],
            chain_id=chain_data["chainId"],
            is_testnet=is_testnet,
            name=name,
            shortcut=shortcut,
            slip44=slip44,
        )
        networks.append(coin)

    return networks


def _build_token(
    complex_token: dict[str, Any], chain_id: int, chain: str
) -> ERC20Token | None:
    # simple validation
    if complex_token["address"][:2] != "0x" or int(complex_token["decimals"]) < 0:
        return None
    try:
        bytes.fromhex(complex_token["address"][2:])
    except ValueError:
        return None

    return ERC20Token(
        address=str(complex_token["address"]).lower(),
        chain=chain,
        chain_id=chain_id,
        decimals=complex_token["decimals"],
        name=complex_token["name"],
        shortcut=complex_token["symbol"],
    )


def _load_erc20_tokens_from_coingecko(
    downloader: Downloader, networks: list[Network]
) -> list[ERC20Token]:
    tokens: list[ERC20Token] = []
    for network in networks:
        network_id = network.get("coingecko_network_id")
        if network_id is None:
            network_id = network.get("coingecko_id")
        if network_id is None:
            continue

        all_tokens = downloader.get_coingecko_tokens_for_network(network_id)

        for token in all_tokens:
            t = _build_token(token, network["chain_id"], network["chain"])
            if t is not None:
                tokens.append(t)

    return tokens


def _load_erc20_tokens_from_repo(networks: list[Network]) -> list[ERC20Token]:
    """Load ERC20 tokens from submodule."""
    tokens: list[ERC20Token] = []
    for network in networks:
        chain_path = TOKENS_PATH / network["chain"]
        for file in chain_path.glob("*.json"):
            token = load_json_file(file)
            t = _build_token(token, network["chain_id"], network["chain"])
            if t is not None:
                tokens.append(t)

    return tokens


def _force_networks_fields_sizes_t1(networks: list[Network]) -> None:
    """Check sizes of embedded network fields for Trezor model 1 based on
    "legacy/firmware/protob/messages-ethereum.options"."""
    # EthereumNetworkInfo.name     max_size:256
    # EthereumNetworkInfo.shortcut max_size:256
    limit = 256
    for network in networks:
        # Cutting of what is over the limit
        if len(network["name"]) > limit:
            logging.info(f"Shortening name in {network}")
            network["name"] = network["name"][:limit]
        if len(network["shortcut"]) > limit:
            logging.info(f"Shortening shortcut in {network}")
            network["shortcut"] = network["shortcut"][:limit]


def _force_tokens_fields_sizes_t1(tokens: list[ERC20Token]) -> None:
    """Check sizes of embeded token fields for Trezor model 1 based on
    "legacy/firmware/protob/messages-ethereum.options"."""
    # EthereumTokenInfo.name    max_size:256
    # EthereumTokenInfo.symbol  max_size:256 (here stored under "shortcut")
    # EthereumTokenInfo.address max_size:20
    limit = 256
    address_bytes_len = 20

    idxs_to_remove: list[int] = []
    for idx, token in enumerate(tokens):
        # Check address length (starts with 0x) and mark token for removal if invalid
        try:
            address_bytes = bytes.fromhex(token["address"][2:])
            if len(address_bytes) != address_bytes_len:
                raise AssertionError
        except (ValueError, AssertionError):
            logging.warning(
                f"\nWARNING: invalid address length - not including {token}."
            )
            idxs_to_remove.append(idx)
            continue

        # Cutting of what is over the limit
        if len(token["name"]) > limit:
            logging.info(f"Shortening name in {token}")
            token["name"] = token["name"][:limit]
        if len(token["shortcut"]) > limit:
            logging.info(f"Shortening shortcut in {token}")
            token["shortcut"] = token["shortcut"][:limit]

    # Remove tokens marked for removal
    idxs_to_remove.sort(reverse=True)
    for idx in idxs_to_remove:
        tokens.pop(idx)


def _build_solana_token(complex_token: dict[str, Any]) -> SolanaToken | None:
    """Build a Solana token from jup.ag data."""
    # simple validation
    if not complex_token.get("address") or not complex_token.get("symbol"):
        return None

    try:
        tools.b58decode(complex_token["address"])
    except Exception as e:
        logging.warning(f"Failed to decode Solana token: {e}")
        return None

    return {
        "mint": complex_token["address"],
        "name": complex_token["name"],
        "shortcut": complex_token["symbol"].upper(),
    }


def _load_solana_tokens_from_coingecko(downloader: Downloader) -> list[SolanaToken]:
    """Load Solana tokens from coingecko API."""
    tokens: list[SolanaToken] = []
    all_tokens = downloader.get_coingecko_tokens_for_network("solana")
    for token in all_tokens:
        t = _build_solana_token(token)
        if t is not None:
            tokens.append(t)
    return tokens


@click.command()
@click.option(
    "-r/-R",
    "--refresh/--no-refresh",
    default=None,
    help="Force refresh or no-refresh data. By default tries to load cached data.",
)
@click.option(
    "-i",
    "--interactive",
    is_flag=True,
    help="Ask about every change in symbols/decimals.",
)
@click.option(
    "--really-apply-all-renames-without-confirmation",
    "force_changes",
    is_flag=True,
    help="Changes to symbols/decimals in definitions should be accepted.",
)
@click.option(
    "-s",
    "--show-all",
    is_flag=True,
    help="Show the differences of all definitions. By default only changes to top 100 definitions (by Coingecko market cap ranking) are shown.",
)
@click.option(
    "-c",
    "--check-builtin",
    is_flag=True,
    help="Compares results with Trezor builtin definitions.",
)
@click.option("-v", "--verbose", is_flag=True, help="Display more info")
def download(
    refresh: bool | None,
    interactive: bool,
    force_changes: bool,
    show_all: bool,
    check_builtin: bool,
    verbose: bool,
) -> None:
    """Download and prepare token definitions."""
    setup_logging(verbose)

    # validating change resolution strategy - max one of the options can be used
    change_strategy = ChangeResolutionStrategy.from_args(
        interactive=interactive,
        force_accept=force_changes,
    )

    # init Ethereum definitions downloader
    downloader = Downloader(refresh)

    networks = _load_ethereum_networks_from_repo()

    # coingecko API
    cg_platforms_json = downloader.get_coingecko_asset_platforms()
    cg_platforms: dict[int, tuple[str, str]] = {}
    for chain in cg_platforms_json:
        # We want only information about chains, that have both chain id and coingecko id,
        # otherwise we could not link local and coingecko networks.
        if chain["chain_identifier"] is not None:
            assert chain["id"] is not None
            assert chain["native_coin_id"] is not None
            cg_platforms[chain["chain_identifier"]] = (
                chain["id"],
                chain["native_coin_id"],
            )

    # defillama API
    dl_chains_json = downloader.get_defillama_chains()
    dl_chains: dict[int, str] = {}
    for chain in dl_chains_json:
        # We want only information about chains, that have both chain id and coingecko id,
        # otherwise we could not link local and coingecko networks.
        if chain["chainId"] is not None and chain["gecko_id"] is not None:
            dl_chains[chain["chainId"]] = chain["gecko_id"]

    # We will try to get as many "coingecko_id"s as possible to be able to use them afterwards
    # to load tokens from coingecko. We won't use coingecko networks, because we don't know which
    # ones are EVM based.
    network_to_cid: dict[str, int] = {}
    native_coin_to_network: dict[str, Network] = {}
    for network in networks:
        # Assign coingecko_id if possible and not there already
        chain_id = network["chain_id"]
        if network.get("coingecko_id") is None:
            # from coingecko via chain_id
            if chain_id in cg_platforms:
                network_id, cg_id = cg_platforms[chain_id]
                network["coingecko_id"] = cg_id
                network["coingecko_network_id"] = network_id
                network_to_cid[network_id] = chain_id
                native_coin_to_network[cg_id] = network
            # from defillama via chain_id
            elif chain_id in dl_chains:
                network["coingecko_network_id"] = dl_chains[chain_id]

        # if we found "coingecko_id" add it to the map - used later to map tokens with coingecko ids
        if (network_id := network.get("coingecko_network_id")) is not None:
            network_to_cid[network_id] = chain_id

    # get tokens
    cg_tokens = _load_erc20_tokens_from_coingecko(downloader, networks)
    repo_tokens = _load_erc20_tokens_from_repo(networks)
    solana_tokens = _load_solana_tokens_from_coingecko(downloader)

    # get data used in further processing now to be able to save cache before we do any
    # token collision process and others
    # get CoinGecko coin list
    cg_coin_list = downloader.get_coingecko_coins_list()
    # get top 100 coins
    cg_top100 = downloader.get_coingecko_top100()
    # save cache
    downloader.save_cache()

    # merge tokens - CoinGecko have precedence, so starting with Ethereum repo first
    token_deduplicator: dict[tuple[int, str], ERC20Token] = {}
    for token in repo_tokens + cg_tokens:
        token_deduplicator[(token["chain_id"], token["address"])] = token
    erc20_tokens = list(token_deduplicator.values())

    # remove items with empty symbol
    networks = [n for n in networks if n["shortcut"]]
    erc20_tokens = [t for t in erc20_tokens if t["shortcut"]]

    # Enforce the maximum field sizes
    _force_networks_fields_sizes_t1(networks)
    _force_tokens_fields_sizes_t1(erc20_tokens)

    # map coingecko ids to tokens
    # NOTE: changes the `tokens` in place!
    tokens_by_chain_id_and_address = {
        (t["chain_id"], t["address"]): t for t in erc20_tokens
    }
    solana_tokens_by_mint = {(t["mint"]): t for t in solana_tokens}
    for cg_coin in cg_coin_list:
        for platform_name, address in cg_coin.get("platforms", {}).items():
            key = (network_to_cid.get(platform_name), address)
            if key in tokens_by_chain_id_and_address:
                tokens_by_chain_id_and_address[key]["coingecko_id"] = cg_coin["id"]
            if platform_name == "solana" and address in solana_tokens_by_mint:
                solana_tokens_by_mint[address]["coingecko_id"] = cg_coin["id"]
        # enrich networks by symbols known from coingecko
        if (network := native_coin_to_network.get(cg_coin["id"])) is not None:
            network["name"] = cg_coin["name"]
            network["shortcut"] = cg_coin["symbol"].upper()

    # get top 100 ids
    cg_top100_ids = {d["id"]: d for d in cg_top100}

    for item in networks + erc20_tokens + solana_tokens:
        if (id := item.get("coingecko_id")) in cg_top100_ids:
            item["coingecko_rank"] = cg_top100_ids[id]["market_cap_rank"]

    if DEFINITIONS_PATH.exists():
        old_defs = load_json_file(DEFINITIONS_PATH)

        def callback():
            DEFINITIONS_PATH.write_text(json.dumps(old_defs, indent=2) + "\n")

        # check networks and tokens
        check_definitions_list(
            old_defs=old_defs["networks"],
            new_defs=networks,
            change_strategy=change_strategy,
            show_all=show_all,
            update_callback=callback,
        )
        check_definitions_list(
            old_defs=old_defs["erc20_tokens"],
            new_defs=erc20_tokens,
            change_strategy=change_strategy,
            show_all=show_all,
            update_callback=callback,
        )
        check_definitions_list(
            old_defs=old_defs["solana_tokens"],
            new_defs=solana_tokens,
            change_strategy=change_strategy,
            show_all=show_all,
            update_callback=callback,
        )

    if check_builtin:
        # check built-in definitions against generated ones
        if not check_builtin_defs(networks, erc20_tokens):
            logging.warning(
                "\nWARNING: Built-in definitions differ from the generated ones."
            )

    # sort networks and tokens
    networks.sort(key=lambda x: x["chain_id"])
    erc20_tokens.sort(key=lambda x: (x["chain_id"], x["address"]))
    solana_tokens.sort(key=lambda x: x["mint"])

    # create definitions data
    definitions_data = DefinitionsData(
        networks=networks,
        erc20_tokens=erc20_tokens,
        solana_tokens=solana_tokens,
    )

    # save results
    metadata = make_metadata(definitions_data)
    store_definitions_data(metadata, definitions_data)
