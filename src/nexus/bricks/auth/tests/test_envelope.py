"""Unit tests for envelope encryption primitives (issue #3803)."""

from __future__ import annotations

import pytest

from nexus.bricks.auth.envelope import AESGCMEnvelope, CiphertextCorrupted


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
