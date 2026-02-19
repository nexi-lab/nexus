"""Factory helpers — adapters, wallet provisioner, resiliency parser."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# =========================================================================
# Issue #1520: NexusFS → FileReaderProtocol adapter
# =========================================================================


class _NexusFSFileReader:
    """Adapts a NexusFS instance to the FileReaderProtocol interface.

    This adapter is the sole coupling point between the kernel (NexusFS)
    and the search brick. Search modules never import NexusFS directly;
    they receive a FileReaderProtocol at composition time.

    Usage::

        from nexus.factory import _NexusFSFileReader

        reader = _NexusFSFileReader(nexus_fs_instance)
        content = reader.read_text("/path/to/file.py")
    """

    def __init__(self, nx: Any) -> None:
        self._nx = nx

    def read_text(self, path: str) -> str:
        content_raw = self._nx.read(path)
        if isinstance(content_raw, bytes):
            return content_raw.decode("utf-8", errors="ignore")
        return str(content_raw)

    def get_searchable_text(self, path: str) -> str | None:
        result: str | None = self._nx.metadata.get_searchable_text(path)
        return result

    def list_files(self, path: str, recursive: bool = True) -> list[Any]:
        result = self._nx.list(path, recursive=recursive)
        items: list[Any] = result.items if hasattr(result, "items") else result
        return items

    def get_session(self) -> Any:
        return self._nx.SessionLocal()

    def get_path_id(self, path: str) -> str | None:
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        with self._nx.SessionLocal() as session:
            stmt = select(FilePathModel.path_id).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            path_id: str | None = session.execute(stmt).scalar_one_or_none()
            return path_id

    def get_content_hash(self, path: str) -> str | None:
        from sqlalchemy import select

        from nexus.storage.models import FilePathModel

        with self._nx.SessionLocal() as session:
            stmt = select(FilePathModel.content_hash).where(
                FilePathModel.virtual_path == path,
                FilePathModel.deleted_at.is_(None),
            )
            content_hash: str | None = session.execute(stmt).scalar_one_or_none()
            return content_hash


def _create_wallet_provisioner() -> Any:
    """Create a sync wallet provisioner for NexusFS agent registration.

    Returns a callable ``(agent_id: str, zone_id: str) -> None`` that creates
    a TigerBeetle wallet account. Returns None if tigerbeetle is not installed.

    Uses the sync TigerBeetle client (``tb.Client``) since NexusFS methods are
    synchronous. The client is lazily created on first call and reused.
    Account creation is idempotent (safe to call multiple times).
    """
    import os

    tb_address = os.environ.get("TIGERBEETLE_ADDRESS", "127.0.0.1:3000")
    tb_cluster = int(os.environ.get("TIGERBEETLE_CLUSTER_ID", "0"))
    pay_enabled = os.environ.get("NEXUS_PAY_ENABLED", "").lower() in ("true", "1", "yes")

    if not pay_enabled:
        logger.debug("[WALLET] NEXUS_PAY_ENABLED not set, wallet provisioner disabled")
        return None

    try:
        import tigerbeetle as _tb  # noqa: F401 — verify availability
    except ImportError:
        logger.debug("[WALLET] tigerbeetle package not installed, wallet provisioner disabled")
        return None

    # Shared state for the closure (lazy client)
    _state: dict[str, Any] = {"client": None}

    def _provision_wallet(agent_id: str, zone_id: str = "default") -> None:
        """Create TigerBeetle account for agent. Idempotent."""
        import tigerbeetle as tb

        from nexus.pay.constants import (
            ACCOUNT_CODE_WALLET,
            LEDGER_CREDITS,
            make_tb_account_id,
        )

        if _state["client"] is None:
            _state["client"] = tb.ClientSync(
                cluster_id=tb_cluster,
                replica_addresses=tb_address,
            )

        tb_id = make_tb_account_id(zone_id, agent_id)
        account = tb.Account(
            id=tb_id,
            ledger=LEDGER_CREDITS,
            code=ACCOUNT_CODE_WALLET,
            flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS,
        )

        client = _state["client"]
        assert client is not None
        errors = client.create_accounts([account])
        # Ignore EXISTS (21) — idempotent operation
        if errors and errors[0].result not in (0, 21):
            raise RuntimeError(f"TigerBeetle account creation failed: {errors[0].result}")

    logger.info("[WALLET] Wallet provisioner enabled (TigerBeetle @ %s)", tb_address)
    return _provision_wallet


def _parse_resiliency_config(raw: dict[str, Any] | None) -> Any:
    """Convert raw YAML dict → frozen ``ResiliencyConfig`` dataclasses.

    Returns default config when *raw* is None or empty.  Falls back to
    default config on parse errors (logs the error).
    """
    from nexus.core.resiliency import (
        CircuitBreakerPolicy,
        ResiliencyConfig,
        RetryPolicy,
        TargetBinding,
        TimeoutPolicy,
        parse_duration,
    )

    if not raw:
        return ResiliencyConfig()

    try:
        timeouts: dict[str, TimeoutPolicy] = {"default": TimeoutPolicy()}
        for name, val in raw.get("timeouts", {}).items():
            if isinstance(val, dict):
                timeouts[name] = TimeoutPolicy(
                    seconds=parse_duration(val.get("seconds", 5.0)),
                )
            else:
                timeouts[name] = TimeoutPolicy(seconds=parse_duration(val))

        retries: dict[str, RetryPolicy] = {"default": RetryPolicy()}
        for name, val in raw.get("retries", {}).items():
            if isinstance(val, dict):
                retries[name] = RetryPolicy(
                    max_retries=int(val.get("max_retries", 3)),
                    max_interval=float(val.get("max_interval", 10.0)),
                    multiplier=float(val.get("multiplier", 2.0)),
                    min_wait=float(val.get("min_wait", 1.0)),
                )

        circuit_breakers: dict[str, CircuitBreakerPolicy] = {"default": CircuitBreakerPolicy()}
        for name, val in raw.get("circuit_breakers", {}).items():
            if isinstance(val, dict):
                circuit_breakers[name] = CircuitBreakerPolicy(
                    failure_threshold=int(val.get("failure_threshold", 5)),
                    success_threshold=int(val.get("success_threshold", 3)),
                    timeout=parse_duration(val.get("timeout", 30.0)),
                )

        targets: dict[str, TargetBinding] = {}
        for name, val in raw.get("targets", {}).items():
            if isinstance(val, dict):
                targets[name] = TargetBinding(
                    timeout=str(val.get("timeout", "default")),
                    retry=str(val.get("retry", "default")),
                    circuit_breaker=str(val.get("circuit_breaker", "default")),
                )

        return ResiliencyConfig(
            timeouts=timeouts,
            retries=retries,
            circuit_breakers=circuit_breakers,
            targets=targets,
        )
    except (ValueError, TypeError, AttributeError) as exc:
        logger.error("Invalid resiliency config, using defaults: %s", exc)
        return ResiliencyConfig()


def _create_distributed_infra(
    dist: Any,
    metadata_store: Any,
    session_factory: Any,
    coordination_url: str | None,
) -> tuple[Any, Any]:
    """Create event bus and lock manager (was NexusFS.__init__ lines 439-521).

    Returns (event_bus, lock_manager) tuple.
    Either event_bus or lock_manager may be None.
    """
    event_bus: Any = None
    lock_manager: Any = None

    try:
        # Initialize lock manager (uses Raft via metadata store)
        if dist.enable_locks:
            from nexus.core.distributed_lock import LockStoreProtocol
            from nexus.raft.lock_manager import (
                RaftLockManager,
                set_distributed_lock_manager,
            )

            if isinstance(metadata_store, LockStoreProtocol):
                lock_manager = RaftLockManager(metadata_store)
                set_distributed_lock_manager(lock_manager)
                logger.info("Distributed lock manager initialized (Raft consensus)")
            else:
                logger.warning(
                    "Distributed locks require LockStoreProtocol-compatible store, got %s. "
                    "Lock manager will not be initialized.",
                    type(metadata_store).__name__,
                )

        # Initialize event bus
        if dist.event_bus_backend == "nats":
            from nexus.services.event_bus.factory import create_event_bus

            event_bus = create_event_bus(
                backend="nats",
                nats_url=dist.nats_url,
                session_factory=session_factory,
            )
            logger.info(
                "Distributed event bus initialized (NATS JetStream: %s, SSOT: PostgreSQL)",
                dist.nats_url,
            )
        elif dist.enable_events:
            import os

            coordination_url_resolved = coordination_url or os.getenv("NEXUS_REDIS_URL")
            event_url_resolved = coordination_url_resolved or os.getenv("NEXUS_DRAGONFLY_URL")
            if event_url_resolved:
                from nexus.cache.dragonfly import DragonflyClient
                from nexus.services.event_bus.redis import RedisEventBus

                event_client = DragonflyClient(url=event_url_resolved)
                event_bus = RedisEventBus(
                    event_client,
                    session_factory=session_factory,
                )
                logger.info(
                    "Distributed event bus initialized (dragonfly: %s, SSOT: PostgreSQL)",
                    event_url_resolved,
                )
    except ImportError as e:
        logger.warning("Could not initialize distributed event system: %s", e)

    return event_bus, lock_manager


def _create_workflow_engine(record_store: Any, glob_match_fn: Any = None) -> Any:
    """Create workflow engine with async store and DI.

    Args:
        record_store: RecordStoreABC instance (has async_session_factory property).
        glob_match_fn: Optional glob match function (Rust glob_fast in production).

    Returns workflow engine or None if unavailable.
    """
    if record_store is None:
        logger.warning("Workflows require record_store, skipping")
        return None
    try:
        from nexus.raft.zone_manager import ROOT_ZONE_ID
        from nexus.storage.models import WorkflowExecutionModel, WorkflowModel
        from nexus.workflows.engine import WorkflowEngine
        from nexus.workflows.protocol import WorkflowServices
        from nexus.workflows.storage import WorkflowStore

        workflow_store = WorkflowStore(
            session_factory=record_store.async_session_factory,
            workflow_model=WorkflowModel,
            execution_model=WorkflowExecutionModel,
            zone_id=ROOT_ZONE_ID,
        )
        services = WorkflowServices(glob_match=glob_match_fn)
        return WorkflowEngine(workflow_store=workflow_store, services=services)
    except Exception as e:
        logger.warning("Failed to create workflow engine: %s", e)
        return None


def _create_provider_registry(parsing: Any) -> Any:
    """Create ProviderRegistry with auto-discovered providers (Issue #657)."""
    from nexus.parsers.providers import ProviderRegistry
    from nexus.parsers.providers.base import ProviderConfig

    registry = ProviderRegistry()
    if parsing is None:
        registry.auto_discover()
        return registry
    parse_providers = [dict(p) for p in parsing.providers] if parsing.providers else None
    if parse_providers:
        configs = [
            ProviderConfig(
                name=p.get("name", "unknown"),
                enabled=p.get("enabled", True),
                priority=p.get("priority", 50),
                api_key=p.get("api_key"),
                api_url=p.get("api_url"),
                supported_formats=p.get("supported_formats"),
            )
            for p in parse_providers
        ]
        registry.auto_discover(configs)
    else:
        registry.auto_discover()
    return registry
