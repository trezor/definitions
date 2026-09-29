from pathlib import Path

from trezorlib import _ed25519 as ed25519
from trezorlib import cosi, definitions
from trezorlib.merkle_tree import MerkleTree

from .common import SIGNATURES_REQUIRED

HERE = Path(__file__).parent

PRIVATE_KEYS_DEV = [byte * 32 for byte in (b"\xdd", b"\xde", b"\xdf")]


def sign_with_dev_keys(root_hash: bytes) -> bytes:
    """Sign the root hash with the development private key."""
    sigmask = (0b111).to_bytes(1, "little")
    # Deterministic nonces keep the signature reproducible across builds;
    # safe only because the dev keys are public knowledge.
    signature = cosi.sign_with_privkeys(
        root_hash, PRIVATE_KEYS_DEV, deterministic=True
    )
    return sigmask + signature


def get_dev_public_key() -> bytes:
    """Compute the CoSi public key for the development private keys."""
    return cosi.combine_keys([cosi.pubkey_from_privkey(sk) for sk in PRIVATE_KEYS_DEV])


def _combine_public_key(sigmask: int, version: int) -> bytes:
    selected_keys = [
        k
        for i, k in enumerate(definitions.DEFINITIONS_PUBLIC_KEYS)
        if sigmask & (1 << i)
    ]
    assert len(selected_keys) >= SIGNATURES_REQUIRED[version]
    return cosi.combine_keys(selected_keys)


def verify_signature(
    signature: bytes, root_hash: bytes, version: int, dev: bool = False
) -> None:
    sigmask, signature = signature[0], signature[1:]
    if dev:
        assert sigmask == 0b111
        public_key = get_dev_public_key()
    else:
        public_key = _combine_public_key(sigmask, version)

    ed25519.checkvalid(signature, root_hash, public_key)


def make_proof(
    serialized: bytes,
    tree: MerkleTree,
    signature: bytes,
) -> bytes:
    proof = tree.get_proof(serialized)
    proof_encoded = definitions.ProofFormat.build(proof)
    return proof_encoded + signature
