"""Loading of Ethereum definition data from its sources.

Sources: the `ethereum-lists` submodule (networks, ERC-20 tokens), the
`clear-signing-erc7730-registry` submodule (ERC-7730 display formats) and the
CoinGecko API (ERC-20 tokens).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import click

from ..check_definitions import check_definitions_list
from ..common import (
    ACTIVE_VERSIONS,
    DEFINITIONS_PATH,
    DISPLAY_FORMATS_LOG_PATH,
    ChangeResolutionStrategy,
    DefinitionsData,
    load_json_file,
    store_definitions_data,
    store_metadata,
)
from ..downloader import Downloader
from ..serialize import make_metadata
from .erc7730 import UnsupportedFeature, load_display_formats
from .types import ERC20DisplayFormat, ERC20Token, Network

ROOT_DIR = Path(__file__).parent.parent.parent
ETHEREUM_LISTS = ROOT_DIR / "ethereum-lists"
ERC7730_REGISTRY = ROOT_DIR / "ethereum" / "clear-signing-erc7730-registry"

NETWORKS_PATH = ETHEREUM_LISTS / "chains" / "_data" / "chains"
TOKENS_PATH = ETHEREUM_LISTS / "tokens" / "tokens"
DISPLAY_FORMATS_PATH = ERC7730_REGISTRY / "registry"

TESTNET_WORDS = ("testnet", "devnet")

# Registry providers (top-level directory names) whose descriptors we emit.
# The whole registry is still scanned so the log inventories what the other
# providers would cost us, but only these feed the emitted records.
ENABLED_PROVIDERS = frozenset(
    {
        "1inch",
        "aave",
        "benqi",
        "consensus-specs",
        "corestake",
        "ethena",
        "hyperliquid",
        "kiln",
        "lido",
        "lifi",
        "lombard",
        "opencover",
        "p2p",
        "sei",
        "tether",
        "weth",
        "yieldxyz",
    }
)


def _get_testnet_status(*strings: str) -> bool:
    for s in strings:
        for testnet in TESTNET_WORDS:
            if testnet in s.lower():
                return True

    return False


# Overrides for networks whose ethereum-lists definition we do not want to
# use. Keyed by chain_id.
NETWORK_OVERRIDES: dict[int, Network] = {
    # Chain id 999 is "Wanchain Testnet" in ethereum-lists, but HyperEVM
    # (Hyperliquid) uses the same chain id and has far more usage, so we
    # define it instead. HyperEVM is not present in ethereum-lists at all.
    999: Network(
        chain="hype",
        chain_id=999,
        is_testnet=False,
        name="HyperEVM",
        shortcut="HYPE",
        slip44=60,
        coingecko_id="hyperliquid",
        coingecko_network_id="hyperevm",
    ),
}

def load_ethereum_networks_from_repo() -> list[Network]:
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

        coin = NETWORK_OVERRIDES.get(chain_data["chainId"]) or Network(
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
    decimals = int(complex_token["decimals"])
    if complex_token["address"][:2] != "0x" or decimals < 0:
        return None
    try:
        bytes.fromhex(complex_token["address"][2:])
    except ValueError:
        return None

    return ERC20Token(
        address=str(complex_token["address"]).lower(),
        chain=chain,
        chain_id=chain_id,
        decimals=decimals,
        name=complex_token["name"],
        shortcut=complex_token["symbol"],
    )


def load_erc20_tokens_from_coingecko(
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


def load_erc20_tokens_from_repo(networks: list[Network]) -> list[ERC20Token]:
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


def force_networks_fields_sizes_t1(networks: list[Network]) -> None:
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


def force_tokens_fields_sizes_t1(tokens: list[ERC20Token]) -> None:
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


def load_display_formats_from_repo(
    networks: list[Network],
) -> list[ERC20DisplayFormat]:
    """Load ERC-7730 calldata display formats from the registry submodule.

    Only `calldata-*.json` files are scanned (the proto's `func_sig` is
    calldata-specific). `common-*.json` files reach us via the `includes`
    merge; `eip712-*.json` files aren't representable in our schema.

    Skips files under `tests/` subdirectories and records for chain_ids we
    don't otherwise know about.

    A display format that uses any feature we can't faithfully represent is
    skipped whole (we never emit one with a field missing). Every such feature,
    and every accepted-but-adjusted field, is collected and written to
    `definitions-latest.log`. The whole registry is scanned for that inventory,
    but only providers in `ENABLED_PROVIDERS` (noted in the log header) feed
    the emitted records.

    Deduplicates on `(chain_id, address, func_sig)`;
    later files override earlier ones.
    """

    known_chain_ids = {n["chain_id"] for n in networks}
    unsupported: list[tuple[str, str, str]] = []
    adjustments: list[tuple[str, str, str]] = []
    loaded: list[tuple[str, bool, list[ERC20DisplayFormat]]] = []

    for path in sorted(DISPLAY_FORMATS_PATH.glob("*/calldata-*.json")):
        if "tests" in path.parts:
            continue

        try:
            # Scan every file so the log covers the whole registry, regardless
            # of which providers are enabled below.
            records = load_display_formats(
                path, unsupported=unsupported, adjustments=adjustments
            )
        except UnsupportedFeature as e:
            # File skipped — its features were already collected into `unsupported`.
            logging.info(f"skipping {path.relative_to(ROOT_DIR)} — {e}")
            continue
        except Exception:
            logging.warning(f"failed to parse {path.relative_to(ROOT_DIR)}")
            raise

        rel = str(path.relative_to(ROOT_DIR))
        # `*/calldata-*.json` glob → the provider is the file's parent directory.
        gated = path.parent.name in ENABLED_PROVIDERS
        loaded.append((rel, gated, records))

    display_formats, conflicts = dedup_display_formats(loaded, known_chain_ids)
    for key_str, overridden, kept in conflicts:
        logging.warning(
            f"display-format override: {kept} redefines {key_str} (was {overridden})"
        )

    if loaded or unsupported:
        write_display_formats_log(unsupported, conflicts, adjustments)
    else:
        # Nothing scanned at all — the registry submodule is most likely
        # uninitialized (e.g. a shallow checkout). Surface the probable cause
        # instead of writing a misleading "0 files" log.
        logging.warning(
            f"no ERC-7730 descriptors found under {DISPLAY_FORMATS_PATH} — is the "
            "clear-signing-erc7730-registry submodule initialized?"
        )
    return display_formats


def dedup_display_formats(
    loaded: list[tuple[str, bool, list[ERC20DisplayFormat]]],
    known_chain_ids: set[int],
) -> tuple[list[ERC20DisplayFormat], list[tuple[str, str, str]]]:
    """Deduplicate display formats on `(chain_id, address, func_sig)`.

    `loaded` is a list of `(source, gated, records)` in processing order. Only
    `gated` records feed the emitted output (later files override earlier ones),
    but conflicts are detected registry-wide: whenever any two files define the
    same key with a *different* payload it's reported as an override conflict
    `(key, overridden_source, kept_source)` — even for not-enabled providers.
    Identical redefinitions are harmless duplicates and not reported.
    """
    dedup: dict[tuple[int, str, str], ERC20DisplayFormat] = {}
    seen: dict[tuple[int, str, str], tuple[ERC20DisplayFormat, str]] = {}
    conflicts: list[tuple[str, str, str]] = []

    for source, gated, records in loaded:
        for r in records:
            if r["chain_id"] not in known_chain_ids:
                continue
            key = (r["chain_id"], r["address"], r["func_sig"])
            prev = seen.get(key)
            if prev is not None and prev[0] != r:
                key_str = f"chain={key[0]} address={key[1]} selector={key[2]}"
                conflicts.append((key_str, prev[1], source))
            seen[key] = (r, source)
            if gated:
                dedup[key] = r

    return list(dedup.values()), conflicts


def _group_by_kind_and_file(
    entries: list[tuple[str, str, str]],
) -> tuple[Counter[str], dict[str, set[str]], dict[str, list[tuple[str, str]]]]:
    """Group `(source, kind, detail)` entries for the two log views."""
    by_kind_count: Counter[str] = Counter(kind for _, kind, _ in entries)
    by_kind_files: dict[str, set[str]] = defaultdict(set)
    by_file: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for source, kind, detail in entries:
        by_kind_files[kind].add(source)
        by_file[source].append((kind, detail))
    return by_kind_count, by_kind_files, by_file


def _grouped_section_lines(
    entries: list[tuple[str, str, str]], kind_header: str
) -> list[str]:
    by_kind_count, by_kind_files, by_file = _group_by_kind_and_file(entries)
    lines = ["", kind_header]
    if not entries:
        lines.append("(none)")
    for kind in sorted(by_kind_count, key=lambda k: (-by_kind_count[k], k)):
        lines.append(
            f"{by_kind_count[kind]:5d}  {len(by_kind_files[kind]):4d} file(s)  {kind}"
        )
    lines += ["", "## By file"]
    for source in sorted(by_file):
        lines.append(source)
        for kind, detail in sorted(by_file[source]):
            lines.append(f"    {kind}: {detail}")
        lines.append("")
    return lines


def write_display_formats_log(
    unsupported: list[tuple[str, str, str]],
    conflicts: list[tuple[str, str, str]],
    adjustments: list[tuple[str, str, str]],
) -> None:
    """Write the ERC-7730 processing log (`definitions-latest.log`).

    Headed by the provider allowlist, then three independent sections: dropped
    display formats grouped by unsupported feature, accepted-but-adjusted
    fields (formatter overrides, ABI retypes, materialized constants), and
    conflicting overrides (the same key redefined differently by multiple
    files). All three cover the whole registry, not just enabled providers.
    """
    enabled = ", ".join(sorted(ENABLED_PROVIDERS)) if ENABLED_PROVIDERS else "(none)"
    affected_files = {src for src, _, _ in unsupported}
    lines = [
        f"# Providers enabled: {enabled}",
        "",
        "# ERC-7730 unsupported features (display format dropped)",
        f"# {len(affected_files)} file(s) affected, "
        f"{len(unsupported)} feature occurrence(s)",
    ]
    lines += _grouped_section_lines(unsupported, "## By feature")

    adjusted_files = {src for src, _, _ in adjustments}
    lines += [
        "",
        "# Adjustments (field accepted with modification)",
        f"# {len(adjusted_files)} file(s), {len(adjustments)} adjustment(s)",
    ]
    lines += _grouped_section_lines(adjustments, "## By kind")

    lines += [
        "",
        "# Conflicting overrides (later file wins)",
        f"# {len(conflicts)} key(s) redefined with a different payload",
        "",
    ]
    if not conflicts:
        lines.append("(none)")
    for key_str, overridden, kept in sorted(conflicts):
        lines.append(key_str)
        lines.append(f"    kept:     {kept}")
        lines.append(f"    overrode: {overridden}")
        lines.append("")

    DISPLAY_FORMATS_LOG_PATH.write_text("\n".join(lines).rstrip() + "\n")
    logging.info(
        f"wrote {DISPLAY_FORMATS_LOG_PATH.name} "
        f"({len(affected_files)} file(s) with drops, {len(adjustments)} adjustment(s), "
        f"{len(conflicts)} conflict(s))"
    )


def update_display_formats_only(
    networks: list[Network],
    change_strategy: ChangeResolutionStrategy,
    show_all: bool,
    show_added: bool,
) -> None:
    """Refresh only the ERC-7730 display formats, leaving the networks, ERC-20 and
    Solana token definitions untouched. No CoinGecko/DeFiLlama calls are made;
    `networks` (loaded from the repo) only supplies the set of known chain ids.
    """
    if not DEFINITIONS_PATH.exists():
        raise click.ClickException(
            f"{DEFINITIONS_PATH} not found — run a full download first."
        )

    display_formats = load_display_formats_from_repo(networks)

    old_defs = load_json_file(DEFINITIONS_PATH)

    def callback():
        DEFINITIONS_PATH.write_text(json.dumps(old_defs, indent=2) + "\n")

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

    display_formats.sort(key=lambda x: (x["chain_id"], x["address"], x["func_sig"]))

    # Reuse the existing networks/tokens/Solana definitions verbatim.
    definitions_data = DefinitionsData(
        networks=old_defs["networks"],
        erc20_tokens=old_defs["erc20_tokens"],
        solana_tokens=old_defs["solana_tokens"],
        erc20_display_formats=display_formats,
    )

    store_definitions_data(definitions_data)
    for version in ACTIVE_VERSIONS:
        metadata = make_metadata(definitions_data, version)
        store_metadata(metadata)
