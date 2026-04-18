"""Unit tests for envelope encryption primitives (issue #3803)."""

from __future__ import annotations

import re

import pytest

from nexus.bricks.auth.envelope import (
    AADMismatch,
    AESGCMEnvelope,
    CiphertextCorrupted,
    DecryptionFailed,
    EnvelopeConfigurationError,
    EnvelopeError,
    WrappedDEKInvalid,
)

# Regex: any base64 or hex blob of 16+ bytes shouldn't appear in error text.
_BLOB_RE = re.compile(r"(?:[A-Za-z0-9+/]{22,}={0,2}|[0-9a-fA-F]{32,})")


class TestAESGCMEnvelope:
    def test_roundtrip(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x00" * 32
        plaintext = b"hello credential"
        aad = b"tenant|principal|id"
        nonce, ciphertext = env.encrypt(dek, plaintext, aad=aad)
        assert len(nonce) == 12
        assert ciphertext != plaintext
        assert env.decrypt(dek, nonce, ciphertext, aad=aad) == plaintext

    def test_wrong_aad_fails(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x01" * 32
        nonce, ct = env.encrypt(dek, b"secret", aad=b"aad-A")
        with pytest.raises(CiphertextCorrupted):
            env.decrypt(dek, nonce, ct, aad=b"aad-B")

    def test_ciphertext_tamper_fails(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x02" * 32
        nonce, ct = env.encrypt(dek, b"secret", aad=b"aad")
        tampered = bytes([ct[0] ^ 0x01]) + ct[1:]
        with pytest.raises(CiphertextCorrupted):
            env.decrypt(dek, nonce, tampered, aad=b"aad")

    def test_fresh_nonce_per_encrypt(self) -> None:
        env = AESGCMEnvelope()
        dek = b"\x03" * 32
        n1, _ = env.encrypt(dek, b"x", aad=b"aad")
        n2, _ = env.encrypt(dek, b"x", aad=b"aad")
        assert n1 != n2

    def test_dek_must_be_32_bytes(self) -> None:
        env = AESGCMEnvelope()
        with pytest.raises(ValueError):
            env.encrypt(b"\x00" * 16, b"x", aad=b"aad")


class TestErrorReprDiscipline:
    def test_all_errors_carry_context_not_secrets(self) -> None:
        import uuid

        tenant = uuid.uuid4()
        pid = "google/alice"
        for cls in (EnvelopeConfigurationError, DecryptionFailed, AADMismatch, WrappedDEKInvalid):
            err = cls.from_row(
                tenant_id=tenant, profile_id=pid, kek_version=7, cause="RuntimeError"
            )
            text = f"{err} || {err!r}"
            assert str(tenant) in text
            assert pid in text
            assert "7" in text
            assert "RuntimeError" in text
            assert _BLOB_RE.search(text) is None, f"{cls.__name__} repr leaked a blob: {text!r}"

    def test_envelope_error_root_is_catchable(self) -> None:
        import uuid

        with pytest.raises(EnvelopeError):
            raise DecryptionFailed.from_row(
                tenant_id=uuid.uuid4(), profile_id="x", kek_version=1, cause="y"
            )
