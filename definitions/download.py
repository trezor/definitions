#!/usr/bin/env python3
"""The `download` command: fetch data from all sources and prepare definitions.

Coin-specific source handling lives in `definitions.<coin>.load`; this module
only orchestrates the pipeline (and merges CoinGecko metadata across coins).
"""

from __future__ import annotations

import json
import logging
import sys

import click

from .check_definitions import check_definitions_list
from .common import (
    DEFINITIONS_PATH,
    ChangeResolutionStrategy,
    DefinitionsData,
    load_json_file,
    setup_logging,
    store_definitions_data,
)
from .downloader import Downloader
from .ethereum.builtin_defs import check_builtin_defs
from .ethereum.load import (
    TOKENS_PATH,
    _force_networks_fields_sizes_t1,
    _force_tokens_fields_sizes_t1,
    _load_display_formats_from_repo,
    _load_erc20_tokens_from_coingecko,
    _load_erc20_tokens_from_repo,
    _load_ethereum_networks_from_repo,
    _update_display_formats_only,
)
from .ethereum.onchain import OnchainDecimalsResolver
from .ethereum.types import ERC20Token, Network
from .serialize import make_metadata
from .solana.load import _load_solana_tokens_from_coingecko


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
    "-a",
    "--show-added",
    is_flag=True,
    help="Show newly added definitions.",
)
@click.option(
    "-c",
    "--check-builtin",
    is_flag=True,
    help="Compares results with Trezor builtin definitions.",
)
@click.option(
    "--sleep-duration",
    type=float,
    default=0,
    help="Amount of seconds to sleep after each download.",
)
@click.option("-v", "--verbose", is_flag=True, help="Display more info")
@click.option(
    "--trace-address",
    default=None,
    help="Trace which source provided token info for this contract address, then exit.",
)
@click.option(
    "--no-onchain-decimals",
    is_flag=True,
    help="Disable verifying conflicting token decimals against the contract on-chain.",
)
@click.option(
    "--erc7730-only",
    is_flag=True,
    help="Refresh only the ERC-7730 display formats from the registry submodule, "
    "reusing the existing networks/tokens/Solana definitions. Skips all CoinGecko "
    "and DeFiLlama downloads.",
)
def download(
    refresh: bool | None,
    interactive: bool,
    force_changes: bool,
    show_all: bool,
    show_added: bool,
    check_builtin: bool,
    verbose: bool,
    sleep_duration: float,
    trace_address: str | None,
    no_onchain_decimals: bool,
    erc7730_only: bool,
) -> None:
    """Download and prepare token definitions."""
    setup_logging(verbose)

    # validating change resolution strategy - max one of the options can be used
    change_strategy = ChangeResolutionStrategy.from_args(
        interactive=interactive,
        force_accept=force_changes,
    )

    networks = _load_ethereum_networks_from_repo()

    if erc7730_only:
        _update_display_formats_only(networks, change_strategy, show_all, show_added)
        return

    # init definitions downloader
    downloader = Downloader(refresh, sleep_duration)

    # coingecko API
    cg_platforms_json = downloader.get_coingecko_asset_platforms()
    cg_platforms: dict[int, tuple[str, str]] = {}
    for chain in cg_platforms_json:
        # We want only information about chains, that have both chain id and coingecko id,
        # otherwise we could not link local and coingecko networks.
        if (
            chain["chain_identifier"] is not None
            and chain["id"] is not None
            and chain["native_coin_id"] is not None
        ):
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

    # get ERC-7730 display formats from the registry submodule
    display_formats = _load_display_formats_from_repo(networks)

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

    if trace_address:
        addr = trace_address.lower()
        print(f"\n=== TRACING ADDRESS: {addr} ===")

        repo_match = next((t for t in repo_tokens if t["address"] == addr), None)
        cg_match = next((t for t in cg_tokens if t["address"] == addr), None)

        if repo_match:
            for network in networks:
                if network["chain_id"] == repo_match["chain_id"]:
                    chain_path = TOKENS_PATH / network["chain"]
                    for f in chain_path.glob("*.json"):
                        data = load_json_file(f)
                        if data.get("address", "").lower() == addr:
                            print(f"  [REPO] File: {f}")
                            print(f"  [REPO] Data: {repo_match}")
                            break
                    break

        if cg_match:
            for network in networks:
                if network["chain_id"] == cg_match["chain_id"]:
                    cg_network_id = network.get("coingecko_network_id") or network.get(
                        "coingecko_id"
                    )
                    print(f"  [COINGECKO] Network ID: {cg_network_id}")
                    print(
                        f"  [COINGECKO] API URL: https://tokens.coingecko.com/{cg_network_id}/all.json"
                    )
                    print(f"  [COINGECKO] Data: {cg_match}")
                    break

        if not repo_match and not cg_match:
            print("  NOT FOUND in any source.")
        else:
            winner_source = "COINGECKO" if cg_match is not None else "REPO"
            print(
                f"\n  >>> FINAL SOURCE: {winner_source} (CoinGecko overrides repo when both present)"
            )

        print("=== END TRACE ===\n")
        sys.exit(0)

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
            network["shortcut"] = cg_coin["symbol"]

    # get top 100 ids
    cg_top100_ids = {d["id"]: d for d in cg_top100}

    for item in networks + erc20_tokens + solana_tokens:
        if (id := item.get("coingecko_id")) in cg_top100_ids:
            item["coingecko_rank"] = cg_top100_ids[id]["market_cap_rank"]

    if DEFINITIONS_PATH.exists():
        old_defs = load_json_file(DEFINITIONS_PATH)

        def callback():
            DEFINITIONS_PATH.write_text(json.dumps(old_defs, indent=2) + "\n")

        decimals_resolver = None if no_onchain_decimals else OnchainDecimalsResolver()

        # check networks and tokens
        check_definitions_list(
            old_defs=old_defs["networks"],
            new_defs=networks,
            change_strategy=change_strategy,
            show_all=show_all,
            show_added=show_added,
            update_callback=callback,
        )
        check_definitions_list(
            old_defs=old_defs["erc20_tokens"],
            new_defs=erc20_tokens,
            change_strategy=change_strategy,
            show_all=show_all,
            show_added=show_added,
            update_callback=callback,
            decimals_resolver=decimals_resolver,
        )
        check_definitions_list(
            old_defs=old_defs["solana_tokens"],
            new_defs=solana_tokens,
            change_strategy=change_strategy,
            show_all=show_all,
            show_added=show_added,
            update_callback=callback,
            main_keys=("mint",),
            def_type="TOKEN",
        )
        check_definitions_list(
            old_defs=old_defs.get("erc20_display_formats", []),
            new_defs=display_formats,
            change_strategy=change_strategy,
            show_all=show_all,
            show_added=show_added,
            update_callback=callback,
            main_keys=("chain_id", "address", "func_sig"),
            def_type="DISPLAY_FORMAT",
            # A wrong display format is worse than a missing one: no tombstones —
            # formats the parser stops emitting are removed and stop being signed.
            only_mark_as_deleted=False,
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
    display_formats.sort(key=lambda x: (x["chain_id"], x["address"], x["func_sig"]))

    # create definitions data
    definitions_data = DefinitionsData(
        networks=networks,
        erc20_tokens=erc20_tokens,
        solana_tokens=solana_tokens,
        erc20_display_formats=display_formats,
    )

    # save results
    metadata = make_metadata(definitions_data)
    store_definitions_data(metadata, definitions_data)
