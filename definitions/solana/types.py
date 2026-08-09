"""Solana-specific definition types (SPL tokens)."""

from __future__ import annotations

import typing as t


class SolanaToken(t.TypedDict):
    mint: str
    name: str
    shortcut: str  # change later to symbol

    coingecko_id: t.NotRequired[str]
    coingecko_rank: t.NotRequired[bool]
    deleted: t.NotRequired[bool]
