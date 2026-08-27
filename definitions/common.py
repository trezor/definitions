"""Coin-agnostic core: file format, payload encoding and shared helpers.

Coin-specific types and serializers live in the per-coin subpackages
(`definitions.ethereum`, `definitions.solana`). The serialization dispatch
itself lives in `definitions.serialize`.
"""

from __future__ import annotations

import dataclasses
import datetime
import io
import json
import logging
import subprocess
import sys
import typing as t
from collections import OrderedDict
from enum import Enum
from hashlib import sha256
from pathlib import Path

import click
from trezorlib import definitions, protobuf

from .ethereum.types import ERC20DisplayFormat, ERC20Token, Network
from .solana.types import SolanaToken

if t.TYPE_CHECKING:
    from typing import TypeVar

    from trezorlib.messages import DefinitionType

    DEFINITION_TYPE = TypeVar(
        "DEFINITION_TYPE", "Network", "ERC20Token", "SolanaToken", "ERC20DisplayFormat"
    )

HERE = Path(__file__).parent
ROOT = HERE.parent

DEFINITIONS_PATH = ROOT / "definitions-latest.json"
DISPLAY_FORMATS_LOG_PATH = ROOT / "definitions-latest.log"
GENERATED_DEFINITIONS_DIR = ROOT / "definitions-latest"

# Definitions format versions the tooling currently produces metadata for.
# Metadata (merkle root, signature) is version-specific, so one metadata file
# per active version is generated. 
ACTIVE_VERSIONS: tuple[int, ...] = (1,)

CURRENT_TIME = datetime.datetime.now(datetime.timezone.utc)
TIMESTAMP_FORMAT = "%d.%m.%Y %X%z"
CURRENT_UNIX_TIMESTAMP = int(CURRENT_TIME.timestamp())
CURRENT_TIMESTAMP_STR = CURRENT_TIME.strftime(TIMESTAMP_FORMAT)

MAGIC = b"trzd"


def metadata_path(version: int) -> Path:
    return ROOT / f"definitions-latest-metadata-v{version}.json"


def validate_version(version: int) -> int:
    if version not in ACTIVE_VERSIONS:
        supported = ", ".join(str(v) for v in ACTIVE_VERSIONS)
        raise click.ClickException(
            f"Unsupported definitions version {version}. "
            f"Supported versions: {supported}."
        )
    return version


def resolve_default_version() -> int:
    if len(ACTIVE_VERSIONS) != 1:
        raise click.ClickException(
            "Multiple definitions versions are active, specify --version. "
            f"Active versions: {', '.join(str(v) for v in ACTIVE_VERSIONS)}."
        )
    return ACTIVE_VERSIONS[0]


class ChangeResolutionStrategy(Enum):
    REJECT_ALL_CHANGES = 1
    ACCEPT_ALL_CHANGES = 2
    PROMPT_USER = 3

    @classmethod
    def from_args(
        cls, interactive: bool, force_accept: bool
    ) -> ChangeResolutionStrategy:
        if interactive and force_accept:
            raise ValueError("Cannot be both interactive and force-accept")

        if interactive:
            return cls.PROMPT_USER
        elif force_accept:
            return cls.ACCEPT_ALL_CHANGES
        else:
            return cls.REJECT_ALL_CHANGES


class DefinitionsFileMetadata(t.TypedDict):
    datetime: str
    unix_timestamp: int
    merkle_root: str
    commit_hash: str
    version: int
    signature: t.NotRequired[str]


class DefinitionsFileFormat(t.TypedDict):
    networks: list[Network]
    erc20_tokens: list[ERC20Token]
    solana_tokens: list[SolanaToken]
    erc20_display_formats: list[ERC20DisplayFormat]


@dataclasses.dataclass
class DefinitionsData:
    networks: list[Network]
    erc20_tokens: list[ERC20Token]
    solana_tokens: list[SolanaToken]
    erc20_display_formats: list[ERC20DisplayFormat]

    @classmethod
    def from_dict(cls, data: DefinitionsFileFormat) -> "DefinitionsData":
        return cls(
            networks=data["networks"],
            erc20_tokens=data["erc20_tokens"],
            solana_tokens=data["solana_tokens"],
            erc20_display_formats=data["erc20_display_formats"],
        )

    def to_dict(self) -> DefinitionsFileFormat:
        return {
            "networks": self.networks,
            "erc20_tokens": self.erc20_tokens,
            "solana_tokens": self.solana_tokens,
            "erc20_display_formats": self.erc20_display_formats,
        }


def setup_logging(verbose: bool):
    log_level = logging.DEBUG if verbose else logging.WARNING
    root = logging.getLogger()
    root.setLevel(log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)
    root.addHandler(handler)


def load_json_file(file: str | Path) -> t.Any:
    return json.loads(Path(file).read_text(), object_pairs_hook=OrderedDict)


def get_git_commit_hash() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()


def hash_dict_on_keys(
    d: Network | ERC20Token | SolanaToken | ERC20DisplayFormat,
    exclude_keys: t.Collection[str] = (),
) -> bytes:
    """Get the hash of a dict, excluding selected keys."""
    tmp_dict = {k: v for k, v in d.items() if k not in exclude_keys}
    return sha256(json.dumps(tmp_dict, sort_keys=True).encode()).digest()


def encode_payload(
    info: protobuf.MessageType,
    data_type_num: DefinitionType,
    timestamp: int,
    version: int,
) -> bytes:
    """Wrap a coin-specific protobuf message into a signed-definition payload."""
    buf = io.BytesIO()
    protobuf.dump_message(buf, info)
    payload = definitions.DefinitionPayload(
        magic=MAGIC,
        version=str(version).encode("ascii"),
        data_type=data_type_num,
        timestamp=timestamp,
        data=buf.getvalue(),
    )
    return payload.build()


def load_definitions_data(
    version: int | None = None,
    *,
    path: Path | None = None,
) -> tuple[DefinitionsFileMetadata, DefinitionsData]:
    """Load definitions data and the metadata of the given format version.

    Coin sections come from `definitions-latest.json`, metadata from
    `definitions-latest-metadata-v<version>.json`. When `version` is None,
    the sole active version is used (must be only one active).
    """
    if version is None:
        version = resolve_default_version()
    validate_version(version)

    if path is None:
        path = DEFINITIONS_PATH
    if not path.is_file():
        raise click.ClickException(
            f'File "{path}" with prepared definitions does not exist.'
        )
    meta_path = metadata_path(version)
    if not meta_path.is_file():
        raise click.ClickException(
            f'File "{meta_path}" with definitions metadata does not exist.'
        )

    defs_data: DefinitionsFileFormat = load_json_file(path)
    metadata: DefinitionsFileMetadata = load_json_file(meta_path)
    if metadata.get("version") != version:
        raise click.ClickException(
            f'Metadata file "{meta_path}" has version {metadata.get("version")!r}, '
            f"expected {version}."
        )
    try:
        definitions_data = DefinitionsData.from_dict(defs_data)
        return metadata, definitions_data
    except KeyError:
        raise click.ClickException(
            "File with prepared definitions is not complete. "
            '"networks", "erc20_tokens", "solana_tokens" and "erc20_display_formats" sections may be missing.'
        )


def _dump_json(path: Path, data: t.Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=1)
        f.write("\n")


def store_definitions_data(
    definitions_data: DefinitionsData,
    *,
    path: Path | None = None,
) -> None:
    if path is None:
        path = DEFINITIONS_PATH
    _dump_json(path, definitions_data.to_dict())
    logging.info(f"Success - results saved under {path}")


def store_metadata(metadata: DefinitionsFileMetadata) -> None:
    meta_path = metadata_path(metadata["version"])
    _dump_json(meta_path, metadata)
    logging.info(f"Success - metadata saved under {meta_path}")
