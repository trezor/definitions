"""Serialization of Solana definitions into Trezor protobuf payloads."""

from __future__ import annotations

from trezorlib import protobuf, tools
from trezorlib.messages import DefinitionType

from ..common import encode_payload
from .types import SolanaToken


class SolanaTokenInfo(protobuf.MessageType):
    MESSAGE_WIRE_TYPE = None
    FIELDS = {
        1: protobuf.Field("mint", "bytes", repeated=False, required=True),
        2: protobuf.Field("symbol", "string", repeated=False, required=True),
        3: protobuf.Field("name", "string", repeated=False, required=True),
    }

    def __init__(
        self,
        *,
        mint: "bytes",
        symbol: "str",
        name: "str",
    ) -> None:
        self.mint = mint
        self.symbol = symbol
        self.name = name


def serialize_token(token: SolanaToken, timestamp: int) -> bytes:
    try:
        token_info = SolanaTokenInfo(
            mint=tools.b58decode(token["mint"]),
            symbol=token["shortcut"],
            name=token["name"],
        )
    except Exception as e:
        print(f"Error serializing solana token: {e}")
        print(token)
        raise e

    return encode_payload(token_info, DefinitionType.SOLANA_TOKEN, timestamp)
