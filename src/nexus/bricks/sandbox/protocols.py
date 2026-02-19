"""Protocol interfaces for Sandbox brick external dependencies.

Defines the contracts that the Sandbox brick requires from external systems.
Concrete implementations are wired by factory/server code at boot time.

Issue #2189: Replace concrete nexus.storage imports with Protocol abstractions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class SandboxRepositoryProtocol(Protocol):
    """Protocol for sandbox metadata persistence (CRUD + queries).

    Concrete implementation: ``SQLAlchemySandboxRepository`` in
    ``nexus.storage.repositories.sandbox``.
    """

    def get_metadata(self, sandbox_id: str) -> dict[str, Any]:
        """Get sandbox metadata by ID. Raises SandboxNotFoundError if not found."""
        ...

    def get_metadata_field(self, sandbox_id: str, field: str) -> Any:
        """Get a single field from sandbox metadata."""
        ...

    def update_metadata(self, sandbox_id: str, **updates: Any) -> dict[str, Any]:
        """Update metadata fields. Returns updated metadata dict."""
        ...

    def create_metadata(
        self,
        sandbox_id: str,
        name: str,
        user_id: str,
        zone_id: str,
        agent_id: str | None,
        provider: str,
        template_id: str | None,
        created_at: datetime,
        last_active_at: datetime,
        ttl_minutes: int,
        expires_at: datetime,
    ) -> dict[str, Any]:
        """Create a new sandbox metadata record. Returns created metadata dict."""
        ...

    def find_active_by_name(self, user_id: str, name: str) -> dict[str, Any] | None:
        """Find an active sandbox by user and name."""
        ...

    def list_sandboxes(
        self,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List sandboxes with optional filtering."""
        ...

    def find_expired(self) -> list[str]:
        """Find IDs of active sandboxes that have expired."""
        ...


class AgentEventLogProtocol(Protocol):
    """Protocol for agent lifecycle event logging.

    Append-only audit log for agent lifecycle events such as sandbox
    creation, connection, and termination.

    Concrete implementation: ``SQLAlchemyAgentEventLog`` in
    ``nexus.storage.repositories.agent_event_log``.
    """

    def record(
        self,
        agent_id: str,
        event_type: str,
        zone_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Record a lifecycle event. Returns the generated event ID."""
        ...

    def list_events(
        self,
        agent_id: str,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List events for an agent, newest first."""
        ...
