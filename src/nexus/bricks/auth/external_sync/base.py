"""Abstract base for external CLI sync adapters.

Sync adapters discover which accounts exist in external CLIs (aws, gcloud, gh)
and produce SyncedProfile metadata. They do NOT resolve actual credentials —
that is ExternalCliBackend's job (external_cli_backend.py).

Each concrete adapter is a thin descriptor subclass of either FileAdapter
(for CLIs with parseable config files) or SubprocessAdapter (for CLIs
that require shell-out). The base classes own all I/O, timeout, retry,
and error-classification logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from nexus.bricks.auth.credential_backend import ResolvedCredential


@dataclass(frozen=True, slots=True)
class SyncedProfile:
    """One discovered account from an external CLI.

    This is metadata only — no secrets. Maps to an AuthProfile in the store
    with backend="external-cli".
    """

    provider: str  # e.g. "s3"
    account_identifier: str  # e.g. "default", "work-prod"
    backend_key: str  # e.g. "aws-cli/default" — opaque to the store
    source: str  # e.g. "aws-cli" — displayed in `auth list` Source column


@dataclass
class SyncResult:
    """Output of a single adapter sync() call."""

    adapter_name: str
    profiles: list[SyncedProfile] = field(default_factory=list)
    error: str | None = None  # non-None means degraded


class ExternalCliSyncAdapter(ABC):
    """Abstract base for external CLI sync adapters.

    Subclass either FileAdapter or SubprocessAdapter, not this directly.
    """

    adapter_name: str  # e.g. "aws-cli", "gcloud"
    sync_ttl_seconds: float = 60.0
    failure_threshold: int = 3
    reset_timeout_seconds: float = 60.0

    @abstractmethod
    async def sync(self) -> SyncResult:
        """Discover all accounts from this external CLI."""
        ...

    @abstractmethod
    async def detect(self) -> bool:
        """Quick check: is this CLI / config available on the system?"""
        ...

    @abstractmethod
    async def resolve_credential(self, backend_key: str) -> ResolvedCredential:
        """Fresh-read a credential for the given backend_key.

        Called by ExternalCliBackend.resolve(). Re-reads the source
        (file or subprocess) and extracts the actual secret for one profile.
        """
        ...

    def resolve_credential_sync(self, backend_key: str) -> ResolvedCredential:
        """Synchronous variant of resolve_credential().

        Default: raises NotImplementedError. Adapters that support sync
        resolution (FileAdapter subclasses, SubprocessAdapter with
        subprocess.run) override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement resolve_credential_sync"
        )
