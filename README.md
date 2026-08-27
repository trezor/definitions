# Definitions

Repository storing external token/network definitions belonging to `Trezor`. It helps by offloading the storage of these data into a client application, so that the device itself does not need to store them (because of flash-size constraints). It also allows for more frequent updates of these definitions. Device requests these data on demand and validates the signature.

## Repository structure

- `cli.py` - entry point for all definition handling (`download`, `generate`, `sign`, ...)
- `definitions/` - Python package with the tooling, split by concern:
  - `common.py` - coin-agnostic core: definitions file format, payload encoding, shared helpers
  - `serialize.py` - serialization dispatch over all coins, Merkle root computation
  - `downloader.py` - coin-agnostic downloading/caching of source data (CoinGecko, DeFiLlama)
  - `download.py` - the `download` command orchestrating the whole pipeline
  - `check_definitions.py`, `generate.py`, `sign.py`, `crypto.py` - coin-agnostic diffing, binary generation and signing
  - `ethereum/` - everything Ethereum-specific: types, serialization, data sources (`ethereum-lists`, ERC-7730 registry, on-chain), built-in definitions check
  - `solana/` - everything Solana-specific: types, serialization, data sources
- `coins_details/` - generation of `coins_details.json` (market/support data per coin)
- `ethereum-lists/`, `ethereum/clear-signing-erc7730-registry`, `coins_details/trezor_common` - data source submodules

When adding definitions for another coin, add a new `definitions/<coin>/` subpackage with its types, serialization and data loading, and wire it into `serialize.py` / `download.py`.

## Update procedure

`./do_update.sh` makes sure to update all definitions to their latest version. It is using data from multiple sources, e.g. `ethereum-lists` repository and `coingecko` API.

This script will automatically create a commit with these changes.

## Signing procedure

To prevent incorrect/malicious definitions from being supplied to `Trezor`, they need to be signed before using them.

Signing has the following steps:
- run `python cli.py computed-merkle-root` to get the `merkle_root` computed from the definitions data rather than trusting the value stored in `definitions-latest-metadata-v1.json::merkle_root`
- sign it with appropriate keys (outside of definitions repo)
- get the signature and provide it as an argument to `do_sign.sh`, e.g. `./do_sign.sh abcd...`
- the results should look something like this signing commit - https://github.com/trezor/definitions/commit/42d3093e83c85dade59af92a37fb3c33d3b047eb
- `definitions.tar.gz` file should also be created, containing signed definitions, ready for deployment

Metadata (merkle root, signature, format version) are version-specific and live in `definitions-latest-metadata-v<version>.json`, separate from the coin data in `definitions-latest.json`.
