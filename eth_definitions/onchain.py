"""On-chain verification of ERC-20 token metadata.

An ERC-20's ``decimals()`` is fixed at deployment and immutable for a given
contract address. A "decimals change" detected by the download pipeline is
therefore never a real on-chain event - it is either stale/wrong metadata being
corrected or two off-chain sources disagreeing. This module asks the contract
itself, which is the only authoritative source.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from .common import load_json_file

HERE = Path(__file__).parent
ROOT = HERE.parent
NETWORKS_PATH = ROOT / "ethereum-lists" / "chains" / "_data" / "chains"

# keccak256("decimals()")[:4] - the ERC-20 decimals() function selector
DECIMALS_SELECTOR = "0x313ce567"


def _load_rpc_urls_for_chain(chain_id: int) -> list[str]:
    """Return usable public HTTPS JSON-RPC endpoints for a chain.

    Skips websocket endpoints and any URL templated with an API-key
    placeholder (``${...}``), which we cannot fill in.
    """
    chain_file = NETWORKS_PATH / f"eip155-{chain_id}.json"
    if not chain_file.exists():
        return []
    data = load_json_file(chain_file)
    urls: list[str] = []
    for url in data.get("rpc", []):
        if not isinstance(url, str):
            continue
        if not url.startswith("https://"):
            continue  # skip wss:// and other schemes
        if "${" in url:
            continue  # skip endpoints requiring an API key
        urls.append(url)
    return urls


class OnchainDecimalsResolver:
    """Resolve a token's decimals by calling ``decimals()`` on the contract.

    Results (including failures) are cached per ``(chain_id, address)`` for the
    lifetime of the instance, so each conflicting token is queried at most once.
    """

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout
        self._rpc_cache: dict[int, list[str]] = {}
        self._result_cache: dict[tuple[int, str], int | None] = {}
        self.session = requests.Session()

    def _rpc_urls(self, chain_id: int) -> list[str]:
        if chain_id not in self._rpc_cache:
            self._rpc_cache[chain_id] = _load_rpc_urls_for_chain(chain_id)
        return self._rpc_cache[chain_id]

    def __call__(self, chain_id: int, address: str) -> int | None:
        key = (chain_id, address.lower())
        if key not in self._result_cache:
            self._result_cache[key] = self._fetch(chain_id, address)
        return self._result_cache[key]

    def _fetch(self, chain_id: int, address: str) -> int | None:
        rpc_urls = self._rpc_urls(chain_id)
        if not rpc_urls:
            logging.warning(
                f"No usable RPC endpoint for chain {chain_id}; "
                f"cannot verify on-chain decimals for {address}."
            )
            return None

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": address, "data": DECIMALS_SELECTOR}, "latest"],
        }
        for url in rpc_urls:
            try:
                r = self.session.post(url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    continue
                result = data.get("result")
                if not result or result == "0x":
                    continue
                value = int(result, 16)
                if 0 <= value <= 255:  # decimals is a uint8
                    logging.info(
                        f"On-chain decimals for {address} on chain {chain_id}: "
                        f"{value} (via {url})"
                    )
                    return value
            except (requests.RequestException, ValueError) as e:
                logging.debug(f"RPC {url} failed for {address}: {e}")
                continue

        logging.warning(
            f"Could not fetch on-chain decimals for {address} on chain {chain_id} "
            f"(tried {len(rpc_urls)} endpoint(s))."
        )
        return None
