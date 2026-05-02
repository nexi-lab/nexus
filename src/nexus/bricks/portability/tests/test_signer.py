"""Tests for ed25519 archive signer."""

import pytest

from nexus.bricks.archive.errors import ArchiveSignatureError
from nexus.bricks.portability.signer import (
    ArchiveSigner,
    canonical_json_bytes,
    load_or_create_keypair,
)


def test_load_or_create_keypair_creates_files(tmp_path):
    key_path = tmp_path / "archive_signing_key"
    priv, pub = load_or_create_keypair(key_path)
    assert key_path.exists()
    assert key_path.with_suffix(".pub").exists()
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert len(priv) == 32  # ed25519 seed
    assert len(pub) == 32


def test_load_or_create_keypair_idempotent(tmp_path):
    key_path = tmp_path / "archive_signing_key"
    priv1, pub1 = load_or_create_keypair(key_path)
    priv2, pub2 = load_or_create_keypair(key_path)
    assert priv1 == priv2
    assert pub1 == pub2


def test_canonical_json_bytes_is_stable():
    a = canonical_json_bytes({"b": 2, "a": 1})
    b = canonical_json_bytes({"a": 1, "b": 2})
    assert a == b
    assert b'"a":1,"b":2' in a


def test_sign_and_verify_round_trip(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    payload = b"manifest-bytes" + b"merkle-root-bytes"
    sig_b64, pub_b64 = signer.sign(payload)
    assert signer.verify(payload, sig_b64, pub_b64) is True


def test_verify_rejects_tampered_payload(tmp_path):
    signer = ArchiveSigner(tmp_path / "k")
    payload = b"original"
    sig_b64, pub_b64 = signer.sign(payload)
    with pytest.raises(ArchiveSignatureError):
        signer.verify(b"tampered", sig_b64, pub_b64)


def test_verify_rejects_wrong_pubkey(tmp_path):
    signer1 = ArchiveSigner(tmp_path / "k1")
    signer2 = ArchiveSigner(tmp_path / "k2")
    sig_b64, _pub1 = signer1.sign(b"payload")
    _sig2, pub2 = signer2.sign(b"payload")
    with pytest.raises(ArchiveSignatureError):
        signer1.verify(b"payload", sig_b64, pub2)
