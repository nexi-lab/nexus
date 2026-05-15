from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from nexus.bricks.auth.daemon.keystore import (
    KeystoreError,
    generate_keypair,
    load_or_create_keypair,
    load_private_key,
    sign_body,
)


def test_generate_creates_0600_file(tmp_path: Path) -> None:
    key_path = tmp_path / "machine.key"
    pub_pem = generate_keypair(key_path)
    assert key_path.exists()
    assert isinstance(serialization.load_pem_public_key(pub_pem), Ed25519PublicKey)
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_load_refuses_bad_perms(tmp_path: Path) -> None:
    key_path = tmp_path / "machine.key"
    generate_keypair(key_path)
    os.chmod(key_path, 0o644)
    with pytest.raises(KeystoreError, match="permissions"):
        load_private_key(key_path)


def test_sign_verify_roundtrip(tmp_path: Path) -> None:
    key_path = tmp_path / "machine.key"
    pub_pem = generate_keypair(key_path)
    priv = load_private_key(key_path)
    body = b"some-canonical-body-bytes"
    sig = sign_body(priv, body)
    pub = serialization.load_pem_public_key(pub_pem)
    assert isinstance(pub, Ed25519PublicKey)
    pub.verify(sig, body)  # raises InvalidSignature if mismatched


def test_load_or_create_is_idempotent(tmp_path: Path) -> None:
    key_path = tmp_path / "machine.key"
    pub1 = load_or_create_keypair(key_path)
    pub2 = load_or_create_keypair(key_path)
    assert pub1 == pub2
