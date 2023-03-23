from pathlib import Path
from typing import Sequence

from trezorlib import cosi

HERE = Path(__file__).parent

PRIVATE_KEYS_DEV = [byte * 32 for byte in (b"\xdd", b"\xde", b"\xdf")]


def sign_with_privkeys(digest: bytes, privkeys: Sequence[bytes]) -> bytes:
    """Locally produce a CoSi signature."""
    pubkeys = [cosi.pubkey_from_privkey(sk) for sk in privkeys]
    nonces = [cosi.get_nonce(sk, digest, i) for i, sk in enumerate(privkeys)]

    global_pk = cosi.combine_keys(pubkeys)
    global_R = cosi.combine_keys(R for _, R in nonces)

    sigs = [
        cosi.sign_with_privkey(digest, sk, global_pk, r, global_R)
        for sk, (r, _) in zip(privkeys, nonces)
    ]

    return cosi.combine_sig(global_R, sigs)


def sign_with_dev_keys(root_hash: bytes) -> bytes:
    """Sign the root hash with the development private key."""
    sigmask = (0b111).to_bytes(1, "little")
    signature = sign_with_privkeys(root_hash, PRIVATE_KEYS_DEV)
    return sigmask + signature


def get_dev_public_key() -> bytes:
    """Compute the CoSi public key for the development private keys."""
    return cosi.combine_keys([cosi.pubkey_from_privkey(sk) for sk in PRIVATE_KEYS_DEV])
