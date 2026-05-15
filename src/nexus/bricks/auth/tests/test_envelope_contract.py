"""Shared contract tests every EncryptionProvider must pass (issue #3803).

Parametrized against InMemoryEncryptionProvider by default. Provider-specific
test modules (Vault, AWS KMS) import these and re-parametrize with their own
``provider_factory`` fixtures gated by ``@pytest.mark.vault`` / ``@pytest.mark.kms``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest

from nexus.bricks.auth.envelope import EncryptionProvider, WrappedDEKInvalid
from nexus.bricks.auth.envelope_providers.in_memory import InMemoryEncryptionProvider


@pytest.fixture()
def provider_factory() -> Callable[[], EncryptionProvider]:
    return InMemoryEncryptionProvider


class EnvelopeProviderContract:
    """Subclassed by provider-specific modules with their own fixture."""

    def test_wrap_unwrap_roundtrip(self, provider_factory) -> None:
        prov = provider_factory()
        dek = b"\x77" * 32
        tid = uuid.uuid4()
        wrapped, version = prov.wrap_dek(dek, tenant_id=tid, aad=b"aad-x")
        assert version >= 1
        assert prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"aad-x", kek_version=version) == dek

    def test_unwrap_with_wrong_tenant_fails(self, provider_factory) -> None:
        prov = provider_factory()
        dek = b"\x78" * 32
        wrapped, v = prov.wrap_dek(dek, tenant_id=uuid.uuid4(), aad=b"aad")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(wrapped, tenant_id=uuid.uuid4(), aad=b"aad", kek_version=v)

    def test_unwrap_with_wrong_aad_fails(self, provider_factory) -> None:
        prov = provider_factory()
        tid = uuid.uuid4()
        wrapped, v = prov.wrap_dek(b"\x79" * 32, tenant_id=tid, aad=b"aad-A")
        with pytest.raises(WrappedDEKInvalid):
            prov.unwrap_dek(wrapped, tenant_id=tid, aad=b"aad-B", kek_version=v)


class TestInMemoryContract(EnvelopeProviderContract):
    """Runs the full contract suite against the in-memory fake."""
