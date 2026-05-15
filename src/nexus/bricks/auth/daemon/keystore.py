"""Ed25519 keystore for daemon to server identity signatures (#3804)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class KeystoreError(Exception):
    """Invalid permissions, missing file, or unreadable key."""


def generate_keypair(path: Path) -> bytes:
    """Create a new Ed25519 keypair at ``path`` (mode 0600). Returns pubkey PEM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Write with exclusive create + 0600
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)
    # belt-and-suspenders chmod in case umask overrode the open() mode
    os.chmod(path, 0o600)

    pub_pem: bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return pub_pem


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """Load with perms check -- reject if mode is looser than 0600."""
    if not path.exists():
        raise KeystoreError(f"keystore not found: {path}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise KeystoreError(f"unsafe permissions on {path}: {oct(mode)} (expected 0600)")
    pem = path.read_bytes()
    priv = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(priv, Ed25519PrivateKey):
        raise KeystoreError("expected Ed25519 private key")
    return priv


def load_or_create_keypair(path: Path) -> bytes:
    """Idempotent: create if missing, return pubkey PEM either way."""
    if path.exists():
        priv = load_private_key(path)
        pub_pem: bytes = priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return pub_pem
    return generate_keypair(path)


def sign_body(priv: Ed25519PrivateKey, body: bytes) -> bytes:
    """Ed25519 signature over canonical bytes."""
    return priv.sign(body)
