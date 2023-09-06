# Definitions

Repository storing external token/network definitions belonging to `Trezor`. It helps by offloading the storage of these data into a client application, so that the device itself does not need to store them (because of flash-size constraints). It also allows for more frequent updates of these definitions. Device requests these data on demand and validates the signature.

## Update procedure

`./do_update.sh` makes sure to update all definitions to their latest version. It is using data from multiple sources, e.g. `ethereum-lists` repository and `coingecko` API.

This script will automatically create a commit with these changes.

## Signing procedure

To prevent incorrect/malicious definitions from being supplied to `Trezor`, they need to be signed before using them.

Signing has the following steps:
- get the `merkle_root` value stored in `definitions-latest.json::metadata::merkle_root` manually or by running `python cli.py current-merkle-root`
- sign it with appropriate keys (outside of definitions repo)
- get the signature and provide it as an argument to `do_sign.sh`, e.g. `./do_sign.sh abcd...`
- the results should look something like this signing commit - https://github.com/trezor/definitions/commit/42d3093e83c85dade59af92a37fb3c33d3b047eb
- `definitions.tar.gz` file should also be created, containing signed definitions, ready for deployment
