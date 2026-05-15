"""Vault Transit provider contract tests (issue #3803).

Gated behind @pytest.mark.vault. Requires a running Vault dev server at
VAULT_ADDR with a transit mount and a derived-context key named
``nexus-test``. See docs/guides/auth-envelope-encryption.md for setup.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from nexus.bricks.auth.envelope import EncryptionProvider
from nexus.bricks.auth.tests.test_envelope_contract import EnvelopeProviderContract

VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "root")
VAULT_TRANSIT_KEY = os.environ.get("VAULT_TRANSIT_KEY", "nexus-test")


def _vault_available() -> bool:
    try:
        import hvac
    except ImportError:
        return False
    try:
        client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
        return bool(client.sys.is_initialized())
    except Exception:
        return False


pytestmark = [
    pytest.mark.vault,
    pytest.mark.skipif(not _vault_available(), reason="Vault dev server not reachable"),
]


@pytest.fixture()
def provider_factory() -> Callable[[], EncryptionProvider]:
    import hvac

    from nexus.bricks.auth.envelope_providers.vault_transit import VaultTransitProvider

    def _make() -> EncryptionProvider:
        client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
        return VaultTransitProvider(client, key_name=VAULT_TRANSIT_KEY)

    return _make


class TestVaultTransitContract(EnvelopeProviderContract):
    """Runs the shared EncryptionProvider contract suite against Vault Transit."""
