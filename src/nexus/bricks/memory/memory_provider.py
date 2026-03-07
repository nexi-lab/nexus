"""MemoryProvider: extracted from NexusFS.memory property (Issue #2033).

Encapsulates lazy Memory / MemoryWithPaging instantiation so that
NexusFS no longer owns the creation logic directly.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.orm import Session


class MemoryProvider:
    """Lazy factory for Memory / MemoryWithPaging instances.

    All heavy imports (``memory_api``, ``memory_with_paging``,
    ``entity_registry``) are deferred to first use so that importing
    this module has zero side-effects.

    Args:
        session_factory: Callable that returns a new SQLAlchemy ``Session``.
        backend: Storage backend forwarded to Memory constructors.
        entity_registry: Pre-built ``EntityRegistry`` (may be ``None``
            — will be lazily created via :meth:`ensure_entity_registry`).
        enable_paging: If ``True``, build ``MemoryWithPaging`` instead of
            plain ``Memory``.
        main_capacity: Main-tier capacity (paging mode only).
        recall_max_age_hours: Recall-tier max age (paging mode only).
        memory_config: Dict with ``zone_id``, ``user_id``, ``agent_id``
            keys used as defaults for the singleton instance.
    """

    def __init__(
        self,
        *,
        session_factory: "Callable[[], Session]",
        backend: Any,
        entity_registry: Any | None,
        enable_paging: bool,
        main_capacity: int,
        recall_max_age_hours: float,
        memory_config: dict[str, Any],
    ) -> None:
        self._session_factory = session_factory
        self._backend = backend
        self._entity_registry = entity_registry
        self._enable_paging = enable_paging
        self._main_capacity = main_capacity
        self._recall_max_age_hours = recall_max_age_hours
        self._memory_config = memory_config

        # Cached singleton (populated by get_or_create)
        self._memory_api: Any | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create(self) -> Any:
        """Return the singleton Memory instance, creating it on first call.

        Mirrors the original ``NexusFS.memory`` property logic.
        """
        if self._memory_api is not None:
            return self._memory_api

        self.ensure_entity_registry()
        session = self._session_factory()

        if self._enable_paging:
            from nexus.bricks.memory.memory_with_paging import MemoryWithPaging

            engine = None
            if self._session_factory is not None:
                engine = getattr(self._session_factory, "kw", {}).get("bind")

            self._memory_api = MemoryWithPaging(
                session=session,
                backend=self._backend,
                zone_id=self._memory_config.get("zone_id"),
                user_id=self._memory_config.get("user_id"),
                agent_id=self._memory_config.get("agent_id"),
                entity_registry=self._entity_registry,
                enable_paging=True,
                main_capacity=self._main_capacity,
                recall_max_age_hours=self._recall_max_age_hours,
                engine=engine,
                session_factory=self._session_factory,
            )
        else:
            from nexus.bricks.memory.service import Memory

            self._memory_api = Memory(
                session=session,
                backend=self._backend,
                zone_id=self._memory_config.get("zone_id"),
                user_id=self._memory_config.get("user_id"),
                agent_id=self._memory_config.get("agent_id"),
                entity_registry=self._entity_registry,
            )

        return self._memory_api

    def get_for_context(self, context: dict[str, Any] | None) -> Any:
        """Return a fresh Memory bound to the given *context*.

        Mirrors the original ``NexusFS._get_memory_api`` logic.

        Args:
            context: Optional dict or ``OperationContext`` with
                ``zone_id``, ``user_id``, ``agent_id`` overrides.
        """
        from nexus.bricks.memory.service import Memory
        from nexus.lib.context_utils import parse_context

        self.ensure_entity_registry()
        session = self._session_factory()
        ctx = parse_context(context)

        # Defaults come from memory_config (which mirrors _default_context
        # values the caller originally supplied).
        return Memory(
            session=session,
            backend=self._backend,
            zone_id=ctx.zone_id or self._memory_config.get("zone_id"),
            user_id=ctx.user_id or self._memory_config.get("user_id"),
            agent_id=ctx.agent_id or self._memory_config.get("agent_id"),
            entity_registry=self._entity_registry,
        )

    def ensure_entity_registry(self) -> Any:
        """Lazily create and cache an ``EntityRegistry``.

        Mirrors the original ``NexusFS._ensure_entity_registry`` logic.
        """
        if self._entity_registry is None:
            import importlib as _il

            _rebac = _il.import_module("nexus.bricks.rebac.entity_registry")
            EntityRegistry = _rebac.EntityRegistry
            self._entity_registry = EntityRegistry(self._session_factory)
        return self._entity_registry


def get_memory_api(nx: Any) -> Any:
    """Get Memory API from a NexusFS instance.

    Replaces the deleted ``NexusFS.memory`` property (Issue #1410 Phase 5).
    Accesses the ``_memory_provider`` (wired via ``bind_wired_services``).

    Args:
        nx: NexusFS instance with ``_memory_provider`` attribute.

    Returns:
        Memory or MemoryWithPaging instance.

    Raises:
        AttributeError: If ``_memory_provider`` is not configured.
    """
    provider = getattr(nx, "_memory_provider", None)
    if provider is None:
        raise AttributeError("Memory provider not configured on NexusFS")
    return provider.get_or_create()
