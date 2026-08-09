"""Loading of Solana definition data from its sources (CoinGecko API)."""

from __future__ import annotations

import logging
from typing import Any

from trezorlib import tools

from ..downloader import Downloader
from .types import SolanaToken


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
