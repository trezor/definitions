"""Serialization dispatch: turn `DefinitionsData` into payload bytes.

The actual per-coin serializers live in `definitions.<coin>.serialize`; this
module only ties them together and derives the Merkle root / file metadata.
"""

from __future__ import annotations

import datetime
import typing as t

from trezorlib.merkle_tree import MerkleTree

from .common import DefinitionsData, DefinitionsFileMetadata, get_git_commit_hash
from .ethereum import serialize as ethereum_serialize
from .ethereum.types import ERC20DisplayFormat, ERC20Token, Network
from .solana import serialize as solana_serialize
from .solana.types import SolanaToken


def get_merkle_root(
    definitions_data: DefinitionsData, timestamp: int, version: int
) -> str:
    serializations = serialize_definitions(definitions_data, timestamp, version)
    merkle_tree = MerkleTree(serializations.keys())
    return merkle_tree.get_root_hash().hex()


def serialize_definitions(
    definitions_data: DefinitionsData,
    timestamp: int,
    version: int,
    progress: t.Callable[[int], None] = lambda _: None,
) -> dict[bytes, Network | ERC20Token | SolanaToken | ERC20DisplayFormat]:
    T = t.TypeVar("T")

    def wrap(i: t.Iterable[T]) -> t.Iterator[T]:
        for item in i:
            yield item
            progress(1)

    network_bytes = {
        ethereum_serialize.serialize_network(n, timestamp, version): n
        for n in wrap(definitions_data.networks)
    }
    erc20_token_bytes = {
        ethereum_serialize.serialize_token(t, timestamp, version): t
        for t in wrap(definitions_data.erc20_tokens)
    }
    solana_token_bytes = {
        solana_serialize.serialize_token(t, timestamp, version): t
        for t in wrap(definitions_data.solana_tokens)
    }
    display_format_bytes = {
        ethereum_serialize.serialize_display_format(df, timestamp, version): df
        for df in wrap(definitions_data.erc20_display_formats)
    }
    return {
        **network_bytes,
        **erc20_token_bytes,
        **solana_token_bytes,
        **display_format_bytes,
    }


def make_metadata(
    definitions_data: DefinitionsData,
    now: datetime.datetime | None = None,
    version: int = 1,
) -> DefinitionsFileMetadata:
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    timestamp = int(now.timestamp())
    time_str = now.isoformat()
    merkle_root = get_merkle_root(definitions_data, timestamp, version)
    return DefinitionsFileMetadata(
        datetime=time_str,
        unix_timestamp=timestamp,
        merkle_root=merkle_root,
        commit_hash=get_git_commit_hash(),
    )
