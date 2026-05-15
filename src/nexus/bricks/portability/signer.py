"""Ed25519 signing for archive manifests (#3793).

Keypair is stored at the path configured for the operator (default
`~/.nexus/archive_signing_key`). Private key file is mode 0600.

Canonical-JSON encoding gives a stable byte representation across Python
versions: keys sorted, no whitespace, ensure_ascii=False.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from nexus.bricks.archive.errors import ArchiveSignatureError


def canonical_json_bytes(obj: object) -> bytes:
    """Return a stable byte encoding for signing/verification."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def load_or_create_keypair(key_path: Path) -> tuple[bytes, bytes]:
    """Load the ed25519 keypair at `key_path`, generating it if missing.

    Returns (private_seed_bytes, public_key_bytes). Both are 32 bytes.
    """
    pub_path = key_path.with_suffix(".pub")
    if key_path.exists():
        with key_path.open("rb") as f:
            priv_seed = f.read()
        priv_key = Ed25519PrivateKey.from_private_bytes(priv_seed)
        pub_bytes = priv_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return priv_seed, pub_bytes

    key_path.parent.mkdir(parents=True, exist_ok=True)
    priv_key = Ed25519PrivateKey.generate()
    priv_seed = priv_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    with key_path.open("wb") as f:
        f.write(priv_seed)
    os.chmod(key_path, 0o600)
    with pub_path.open("wb") as f:
        f.write(pub_bytes)
    return priv_seed, pub_bytes


class ArchiveSigner:
    """Sign and verify archive payloads with ed25519."""

    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._priv_seed, self._pub_bytes = load_or_create_keypair(key_path)

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self._pub_bytes).decode("ascii")

    def sign(self, payload: bytes) -> tuple[str, str]:
        """Sign `payload`. Returns (signature_b64, signer_pubkey_b64)."""
        priv = Ed25519PrivateKey.from_private_bytes(self._priv_seed)
        sig = priv.sign(payload)
        return base64.b64encode(sig).decode("ascii"), self.public_key_b64

    @staticmethod
    def verify(payload: bytes, signature_b64: str, pubkey_b64: str) -> bool:
        """Verify `signature_b64` over `payload` with `pubkey_b64`.

        Returns True on success, raises ArchiveSignatureError on failure.
        """
        try:
            sig = base64.b64decode(signature_b64)
            pub_bytes = base64.b64decode(pubkey_b64)
            pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
            pub.verify(sig, payload)
        except (InvalidSignature, ValueError) as e:
            raise ArchiveSignatureError(f"signature verify failed: {e}") from e
        return True


__all__ = ["ArchiveSigner", "canonical_json_bytes", "load_or_create_keypair"]
