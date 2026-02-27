"""Memory service factory — create_memory_service for server-layer RPC."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _resolve_embedding_provider() -> Any:
    """Resolve the embedding provider from the search brick (factory-layer DI)."""
    # Removed: txtai handles this (Issue #2663)
    # embeddings module was deleted; always return None.
    return None


def _resolve_graph_store_class() -> type[Any] | None:
    """Resolve the GraphStore class from the search brick (factory-layer DI)."""
    # Removed: txtai handles this (Issue #2663)
    # graph_store module was deleted; always return None.
    return None


def _resolve_vector_db(engine: Any) -> Any:  # noqa: ARG001
    """Resolve VectorDatabase from the search brick (factory-layer DI)."""
    # Removed: txtai handles this (Issue #2663)
    # vector_db module was deleted; always return None.
    return None


def create_memory_service(nx: Any) -> Any:
    """Create MemoryService for server-layer RPC dispatch (Issue #12).

    This is a **server-layer** factory function — the kernel (NexusFS) has
    zero knowledge of MemoryService.  The server calls this once during
    ``create_app()`` and registers the result as an additional RPC source.

    Args:
        nx: A NexusFS instance (used to read SessionLocal, backend, config).

    Returns:
        MemoryService instance, or None if dependencies are unavailable.
    """
    try:
        memory_cfg = getattr(nx, "_memory_config_obj", None)
        _paging = getattr(memory_cfg, "enable_paging", False) if memory_cfg else False
        _main_cap = getattr(memory_cfg, "main_capacity", 1000) if memory_cfg else 1000
        _recall_age = getattr(memory_cfg, "recall_max_age_hours", 168) if memory_cfg else 168

        # --- DI: resolve cross-brick dependencies at the factory layer ---
        _embedding_provider = _resolve_embedding_provider()
        _graph_store_class = _resolve_graph_store_class()

        def _create_memory(
            zone_id: str | None = None,
            user_id: str | None = None,
            agent_id: str | None = None,
            use_paging: bool | None = None,
        ) -> Any:
            nx._memory_provider.ensure_entity_registry()
            session = nx.SessionLocal()
            do_paging = use_paging if use_paging is not None else _paging

            if do_paging:
                from nexus.bricks.memory.memory_with_paging import MemoryWithPaging

                engine = nx.SessionLocal.kw.get("bind") if nx.SessionLocal else None
                vector_db = _resolve_vector_db(engine)
                return MemoryWithPaging(
                    session=session,
                    backend=nx.backend,
                    zone_id=zone_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    entity_registry=nx._entity_registry,
                    enable_paging=True,
                    main_capacity=_main_cap,
                    recall_max_age_hours=_recall_age,
                    engine=engine,
                    session_factory=nx.SessionLocal,
                    vector_db=vector_db,
                )
            else:
                from nexus.bricks.memory.service import Memory

                return Memory(
                    session=session,
                    backend=nx.backend,
                    zone_id=zone_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    entity_registry=nx._entity_registry,
                    embedding_provider=_embedding_provider,
                    graph_store_class=_graph_store_class,
                )

        from nexus.services.memory_service import MemoryService

        default_ctx = getattr(nx, "_default_context", None)
        if default_ctx is None:
            from nexus.contracts.types import OperationContext

            default_ctx = OperationContext(user_id="system", groups=[])
        memory_config: dict[str, str | None] = {
            "zone_id": getattr(default_ctx, "zone_id", None),
            "user_id": getattr(default_ctx, "user_id", None),
            "agent_id": getattr(default_ctx, "agent_id", None),
        }

        svc = MemoryService(
            memory_factory=_create_memory,
            session_factory=nx.SessionLocal,
            backend=nx.backend,
            default_context=default_ctx,
            memory_config=memory_config,
        )
        logger.info("[FACTORY] MemoryService created for server-layer RPC")
        return svc
    except Exception as exc:
        logger.debug("[FACTORY] MemoryService unavailable: %s", exc)
        return None
