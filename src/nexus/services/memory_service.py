"""Memory Service — extracted from NexusFS kernel (Issue #12).

Encapsulates:
- Memory CRUD operations (store, list, query, retrieve, delete, approve, deactivate)

All service-layer imports (Memory, MemoryWithPaging) stay here — the kernel
never sees them.

RPC methods are decorated with ``@rpc_expose`` so they are auto-discovered
by ``_discover_exposed_methods()`` when passed as an additional source.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.types import OperationContext
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _parse_context(
    context: OperationContext | dict | None,
    _default_context: OperationContext,
) -> OperationContext:
    """Parse context dict or OperationContext into OperationContext."""
    if isinstance(context, OperationContext):
        return context
    if context is None:
        context = {}
    return OperationContext(
        user_id=context.get("user_id", "system"),
        groups=context.get("groups", []),
        zone_id=context.get("zone_id"),
        agent_id=context.get("agent_id"),
        is_admin=context.get("is_admin", False),
        is_system=context.get("is_system", False),
    )


class MemoryService:
    """Memory operations (SYNC).

    Wraps Memory/MemoryWithPaging creation and all memory CRUD
    operations.  Constructed in
    ``factory._boot_wired_services()`` and registered as an additional
    RPC source in ``fastapi_server._discover_exposed_methods()``.

    The kernel never imports or calls this service.
    """

    def __init__(
        self,
        memory_factory: "Callable[..., Any]",
        session_factory: Any,
        backend: Any,
        default_context: OperationContext,
        memory_config: dict[str, str | None],
    ) -> None:
        self._factory = memory_factory
        self._session_factory = session_factory
        self._backend = backend
        self._default_context = default_context
        self._memory_config = memory_config
        self._cached_api: Any = None

    # ── Memory API accessors ────────────────────────────────────────

    def get_default(self) -> Any:
        """Get or create the cached default Memory instance."""
        if self._cached_api is None:
            self._cached_api = self._factory(
                zone_id=self._memory_config.get("zone_id"),
                user_id=self._memory_config.get("user_id"),
                agent_id=self._memory_config.get("agent_id"),
            )
        return self._cached_api

    def get_for_context(self, context: OperationContext | dict | None = None) -> Any:
        """Create a fresh Memory instance with context-specific identity."""
        ctx = _parse_context(context, self._default_context)
        return self._factory(
            zone_id=ctx.zone_id or self._default_context.zone_id,
            user_id=ctx.user_id or self._default_context.user_id,
            agent_id=ctx.agent_id or self._default_context.agent_id,
            use_paging=False,
        )

    def _get_memory_api_with_context(self, context: Any) -> Any:
        """Get Memory API instance with authenticated context.

        Builds a context dict from an OperationContext and delegates to
        ``get_for_context()``.
        """
        context_dict: dict[str, Any] = {}
        if context:
            if hasattr(context, "zone_id") and context.zone_id:
                context_dict["zone_id"] = context.zone_id
            if hasattr(context, "user_id") and context.user_id:
                context_dict["user_id"] = context.user_id
            if hasattr(context, "agent_id") and context.agent_id:
                context_dict["agent_id"] = context.agent_id

        return self.get_for_context(context_dict if context_dict else None)

    # ── Memory CRUD operations (moved from handlers/memory.py) ──────

    @rpc_expose(description="Store a memory record")
    def store_memory(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "agent",
        importance: float = 0.5,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        tags: list[str] | None = None,  # noqa: ARG002
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Store a memory record."""
        memory_api = self._get_memory_api_with_context(context)
        memory_id = memory_api.store(
            content=content,
            memory_type=memory_type,
            scope=scope,
            importance=importance,
            namespace=namespace,
            path_key=path_key,
            state=state,
        )
        return {"memory_id": memory_id}

    @rpc_expose(description="List memory records")
    def list_memories(
        self,
        limit: int = 50,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List memory records."""
        memory_api = self._get_memory_api_with_context(context)
        memories = memory_api.list(
            scope=scope,
            memory_type=memory_type,
            namespace=namespace,
            namespace_prefix=namespace_prefix,
            state=state,
            limit=limit,
        )
        return {"memories": memories}

    @rpc_expose(description="Query or search memory records")
    def query_memories(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        state: str | None = "active",
        limit: int = 50,
        query: str | None = None,
        search_mode: str | None = None,
        embedding_provider: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Query or search memory records."""
        memory_api = self._get_memory_api_with_context(context)

        if query:
            embedding_provider_obj = None
            if embedding_provider:
                try:
                    import importlib as _il

                    create_embedding_provider = _il.import_module(
                        "nexus.bricks.search.embeddings"
                    ).create_embedding_provider
                    embedding_provider_obj = create_embedding_provider(provider=embedding_provider)
                except Exception as e:
                    logger.debug(
                        "Failed to create embedding provider %s: %s", embedding_provider, e
                    )

            memories = memory_api.search(
                query=query,
                memory_type=memory_type,
                scope=scope,
                limit=limit,
                search_mode=search_mode or "hybrid",
                embedding_provider=embedding_provider_obj,
            )
        else:
            memories = memory_api.query(
                memory_type=memory_type,
                scope=scope,
                state=state,
                limit=limit,
            )
        return {"memories": memories}

    @rpc_expose(description="Retrieve a memory record by namespace/path")
    def retrieve_memory(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Retrieve a memory record."""
        memory_api = self._get_memory_api_with_context(context)
        memory = memory_api.retrieve(
            namespace=namespace,
            path_key=path_key,
            path=path,
        )
        return {"memory": memory}

    @rpc_expose(description="Delete a memory record")
    def delete_memory(
        self,
        memory_id: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Delete a memory record."""
        memory_api = self._get_memory_api_with_context(context)
        deleted = memory_api.delete(memory_id)
        return {"deleted": deleted}

    @rpc_expose(description="Approve a memory record")
    def approve_memory(
        self,
        memory_id: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Approve a memory record."""
        memory_api = self._get_memory_api_with_context(context)
        approved = memory_api.approve(memory_id)
        return {"approved": approved}

    @rpc_expose(description="Deactivate a memory record")
    def deactivate_memory(
        self,
        memory_id: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Deactivate a memory record."""
        memory_api = self._get_memory_api_with_context(context)
        deactivated = memory_api.deactivate(memory_id)
        return {"deactivated": deactivated}

    @rpc_expose(description="Approve a batch of memory records")
    def approve_memory_batch(
        self,
        memory_ids: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Approve a batch of memory records."""
        memory_api = self._get_memory_api_with_context(context)
        result: dict[str, Any] = memory_api.approve_batch(memory_ids)
        return result

    @rpc_expose(description="Deactivate a batch of memory records")
    def deactivate_memory_batch(
        self,
        memory_ids: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Deactivate a batch of memory records."""
        memory_api = self._get_memory_api_with_context(context)
        result: dict[str, Any] = memory_api.deactivate_batch(memory_ids)
        return result

    @rpc_expose(description="Delete a batch of memory records")
    def delete_memory_batch(
        self,
        memory_ids: list[str],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Delete a batch of memory records."""
        memory_api = self._get_memory_api_with_context(context)
        result: dict[str, Any] = memory_api.delete_batch(memory_ids)
        return result

    # ── Lifecycle ───────────────────────────────────────────────────

    def close(self) -> None:
        """Close the cached Memory session."""
        if self._cached_api is not None and hasattr(self._cached_api, "session"):
            try:
                self._cached_api.session.close()
            except Exception as e:
                logger.debug("Failed to close memory API session: %s", e)
