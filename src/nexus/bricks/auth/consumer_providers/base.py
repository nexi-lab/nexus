"""ProviderAdapter Protocol — provider-specific envelope-payload decoders.

Each adapter is pure deserialization: takes envelope plaintext bytes (JSON,
provider-shape), returns a ``MaterializedCredential``. No network calls, no
state. Adapters are registered in ``consumer_providers/__init__.py``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from nexus.bricks.auth.consumer import MaterializedCredential


@runtime_checkable
class ProviderAdapter(Protocol):
    """Pure-function interface: envelope plaintext → MaterializedCredential."""

    name: str  # "aws" | "github" | future providers

    def materialize(self, plaintext_payload: bytes) -> MaterializedCredential:
        """Decode envelope plaintext into a MaterializedCredential.

        Raises:
            ValueError | KeyError on malformed payload — CredentialConsumer
            wraps these as AdapterMaterializeFailed before exiting.
        """
        ...
