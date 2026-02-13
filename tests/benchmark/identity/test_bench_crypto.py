"""Performance benchmarks for identity crypto operations (Issue #1355, Decision #10B).

Targets:
- Key generation: <10ms
- DID encoding: <1ms
- JWS signing: <5ms
- JWS verification: <10ms

Run with: pytest tests/benchmark/identity/ -m benchmark --benchmark-only
"""

from __future__ import annotations

import pytest

from nexus.identity.crypto import IdentityCrypto
from nexus.identity.did import create_did_key, resolve_did_key


@pytest.fixture
def crypto() -> IdentityCrypto:
    return IdentityCrypto()


@pytest.fixture
def keypair(crypto: IdentityCrypto):
    return crypto.generate_keypair()


@pytest.fixture
def message() -> bytes:
    return b"benchmark test message for identity verification"


@pytest.mark.benchmark(group="identity-crypto")
class TestCryptoBenchmarks:
    def test_generate_keypair(self, benchmark, crypto: IdentityCrypto) -> None:
        """Target: <10ms per keypair generation."""
        benchmark(crypto.generate_keypair)

    def test_sign_message(self, benchmark, crypto: IdentityCrypto, keypair, message: bytes) -> None:
        """Target: <5ms per signature."""
        private, _ = keypair
        benchmark(crypto.sign, message, private)

    def test_verify_signature(
        self, benchmark, crypto: IdentityCrypto, keypair, message: bytes
    ) -> None:
        """Target: <10ms per verification."""
        private, public = keypair
        signature = crypto.sign(message, private)
        benchmark(crypto.verify, message, signature, public)

    def test_public_key_to_bytes(self, benchmark, keypair) -> None:
        """Target: <0.1ms."""
        _, public = keypair
        benchmark(IdentityCrypto.public_key_to_bytes, public)

    def test_public_key_from_bytes(self, benchmark, keypair) -> None:
        """Target: <0.1ms."""
        _, public = keypair
        raw = IdentityCrypto.public_key_to_bytes(public)
        benchmark(IdentityCrypto.public_key_from_bytes, raw)


@pytest.mark.benchmark(group="identity-did")
class TestDIDBenchmarks:
    def test_create_did_key(self, benchmark, keypair) -> None:
        """Target: <1ms per DID generation."""
        _, public = keypair
        benchmark(create_did_key, public)

    def test_resolve_did_key(self, benchmark, keypair) -> None:
        """Target: <1ms per DID resolution."""
        _, public = keypair
        did = create_did_key(public)
        benchmark(resolve_did_key, did)
