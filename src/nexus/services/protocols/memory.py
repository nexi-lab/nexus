"""Memory service protocol (ops-scenario-matrix S21: Agent Memory).

Defines the contract for AI agent memory management — storing, querying,
searching, and managing lifecycle of agent memories with identity-based
relationships and semantic search.

Storage Affinity: **RecordStore** (relational memory records) +
                  **ObjectStore** (CAS content blobs) +
                  **CacheStore** (embedding / search index).

References:
    - docs/architecture/ops-scenario-matrix.md  (S21)
    - docs/architecture/data-storage-matrix.md  (Four Pillars)
    - Issue #1287: Extract NexusFS domain services from god object
"""

import builtins
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable


@runtime_checkable
class MemoryProtocol(Protocol):
    """Service contract for AI agent memory management.

    Covers core CRUD, state lifecycle, versioning, search, and maintenance.

    Note: ``builtins.list`` is used for type annotations because the ``list``
    method on this protocol shadows the builtin ``list`` type.
    """

    # ── Core CRUD ──────────────────────────────────────────────────────

    def store(
        self,
        content: str | bytes | dict[str, Any],
        scope: str = "user",
        memory_type: str | None = None,
        importance: float | None = None,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        _metadata: dict[str, Any] | None = None,
        context: Any | None = None,
        generate_embedding: bool = True,
        embedding_provider: Any = None,
        resolve_coreferences: bool = False,
        coreference_context: str | None = None,
        resolve_temporal: bool = False,
        temporal_reference_time: Any = None,
        extract_entities: bool = True,
        extract_temporal: bool = True,
        extract_relationships: bool = False,
        relationship_types: builtins.list[str] | None = None,
        store_to_graph: bool = False,
        valid_at: datetime | str | None = None,
        classify_stability: bool = True,
        detect_evolution: bool = False,
    ) -> str: ...

    def get(
        self,
        memory_id: str,
        track_access: bool = True,
        context: Any | None = None,
    ) -> dict[str, Any] | None: ...

    def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None: ...

    def delete(
        self,
        memory_id: str,
        context: Any | None = None,
    ) -> bool: ...

    def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        during: str | None = None,
        limit: int | None = 100,
        context: Any | None = None,
    ) -> builtins.list[dict[str, Any]]: ...

    def query(
        self,
        user_id: str | None = None,
        agent_id: str | None = None,
        zone_id: str | None = None,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        during: str | None = None,
        entity_type: str | None = None,
        person: str | None = None,
        event_after: str | datetime | None = None,
        event_before: str | datetime | None = None,
        include_invalid: bool = False,
        include_superseded: bool = False,
        temporal_stability: str | None = None,
        as_of: str | datetime | None = None,
        as_of_event: str | datetime | None = None,
        as_of_system: str | datetime | None = None,
        limit: int | None = None,
        offset: int = 0,
        context: Any | None = None,
    ) -> builtins.list[dict[str, Any]]: ...

    def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        search_mode: str = "hybrid",
        embedding_provider: Any = None,
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        during: str | None = None,
    ) -> builtins.list[dict[str, Any]]: ...

    # ── State lifecycle ────────────────────────────────────────────────

    def approve(self, memory_id: str) -> bool: ...

    def deactivate(self, memory_id: str) -> bool: ...

    def approve_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]: ...

    def deactivate_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]: ...

    def delete_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]: ...

    def invalidate(
        self,
        memory_id: str,
        invalid_at: datetime | str | None = None,
    ) -> bool: ...

    def invalidate_batch(
        self,
        memory_ids: builtins.list[str],
        invalid_at: datetime | str | None = None,
    ) -> dict[str, Any]: ...

    def revalidate(self, memory_id: str) -> bool: ...

    # ── Versioning ─────────────────────────────────────────────────────

    def get_history(self, memory_id: str) -> builtins.list[dict[str, Any]]: ...

    def list_versions(self, memory_id: str) -> builtins.list[dict[str, Any]]: ...

    def get_version(
        self,
        memory_id: str,
        version: int,
        context: Any | None = None,
    ) -> dict[str, Any] | None: ...

    def rollback(
        self,
        memory_id: str,
        version: int,
        context: Any | None = None,
    ) -> None: ...

    def diff_versions(
        self,
        memory_id: str,
        v1: int,
        v2: int,
        mode: Literal["metadata", "content"] = "metadata",
        context: Any | None = None,
    ) -> dict[str, Any] | str: ...

    def gc_old_versions(self, older_than_days: int = 365) -> int: ...

    def resolve_to_current(self, memory_id: str) -> Any: ...

    # ── Maintenance ────────────────────────────────────────────────────

    def apply_decay_batch(
        self,
        decay_factor: float = 0.95,
        min_importance: float = 0.1,
        batch_size: int = 1000,
    ) -> dict[str, Any]: ...

    # ── Path resolution (Issue #2177) ───────────────────────────────

    @staticmethod
    def is_memory_path(path: str) -> bool: ...

    def resolve(self, virtual_path: str) -> Any: ...


# ── Narrow dependency Protocols for Memory brick (Issue #2190) ─────────


@runtime_checkable
class MemoryPermissionCheckerProtocol(Protocol):
    """Narrow permission checker for Memory brick (Issue #2190).

    Decouples Memory from the concrete MemoryPermissionEnforcer / ReBAC
    implementation.  Memory only needs ``check_memory()`` — not the full
    PermissionProtocol surface.

    # PERF: no isinstance() in hot path (Issue #1291)
    """

    def check_memory(self, memory: Any, permission: Any, context: Any) -> bool: ...


@runtime_checkable
class EntityResolverProtocol(Protocol):
    """Narrow entity resolution for Memory brick (Issue #2190).

    Decouples Memory from the concrete EntityRegistry / ReBAC
    implementation.  Memory only calls ``extract_ids_from_path_parts``.

    # PERF: no isinstance() in hot path (Issue #1291)
    """

    def extract_ids_from_path_parts(self, parts: builtins.list[str]) -> dict[str, str]: ...
