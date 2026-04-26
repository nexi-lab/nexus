"""Boot Tier 2b (POST-KERNEL) — services needing NexusFS reference."""

import logging
import time
from collections.abc import Callable
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID

logger = logging.getLogger(__name__)


def _boot_post_kernel_services(
    nx: Any,
    services: dict[str, Any],
    svc_on: Callable[[str], bool] | None = None,
    *,
    security_config: Any = None,
) -> dict[str, Any]:
    """Boot Tier 2b (WIRED) — services needing NexusFS reference.

    Two-phase init: called AFTER NexusFS construction in ``create_nexus_fs()``.
    ``NexusFSGateway`` breaks the circular dependency between kernel and services.

    Profile gating is applied via ``svc_on`` — same callback used by other tiers.
    Services that fail to construct are set to None (degraded mode).

    Args:
        nx: The NexusFS instance (already constructed).
        services: Unified services dict (all tiers merged).
        svc_on: Callable ``(name: str) -> bool`` for profile-based gating.

    Returns:
        Dict mapping service field names to instances (some may be None).
    """
    from nexus.factory._helpers import _make_gate

    t0 = time.perf_counter()
    _on = _make_gate(svc_on)

    # All backends are Rust-native now — no Python root backend object available.
    _root_backend: Any = None

    # --- NexusFSGateway: adapter breaking circular dep (Issue #1287) ---
    gateway: Any = None
    try:
        from nexus.services.gateway import NexusFSGateway

        gateway = NexusFSGateway(nx)
        logger.debug("[BOOT:WIRED] NexusFSGateway created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] NexusFSGateway unavailable: %s", exc)

    # --- IPC: deferred to initialize() via _initialize_wired_ipc() ---

    # --- ReBACService: Permission and access control operations ---
    rebac_service: Any = None
    try:
        from nexus.bricks.rebac.rebac_service import ReBACService

        rebac_service = ReBACService(
            rebac_manager=services.get("rebac_manager"),
            enforce_permissions=nx._perm_config.enforce,
            enable_audit_logging=True,
            circuit_breaker=services.get("rebac_circuit_breaker"),
            file_reader=nx.sys_read,
            permission_enforcer=services.get("permission_enforcer"),
        )
        logger.debug("[BOOT:WIRED] ReBACService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] ReBACService unavailable: %s", exc)

    # --- OAuthCredentialService: OAuth credential lifecycle (brick) ---
    # Created before MCPService because mcp_connect() needs credential_service.
    oauth_service: Any = None
    if _on("sandbox"):
        try:
            import os

            from nexus.bricks.auth.oauth.credential_service import OAuthCredentialService

            oauth_service = OAuthCredentialService(
                oauth_factory=None,
                token_manager=None,
                database_url=os.getenv("TOKEN_MANAGER_DB"),
                oauth_config=getattr(nx._config, "oauth", None) if nx._config else None,
            )
            logger.debug("[BOOT:WIRED] OAuthCredentialService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] OAuthCredentialService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] OAuthCredentialService disabled by profile")

    # --- MCPService: Model Context Protocol operations ---
    mcp_service: Any = None
    if _on("mcp"):
        try:
            from nexus.bricks.mcp.mcp_service import MCPService

            def _list_mount_labels() -> list[tuple[str, str]]:
                return [(mp, "mounted") for mp in nx._driver_coordinator.mount_points()]

            mcp_service = MCPService(
                filesystem=nx,
                credential_service=oauth_service,
                mount_lister=_list_mount_labels,
                ssrf_config=(
                    getattr(security_config, "ssrf", None) if security_config is not None else None
                ),
            )
            logger.debug("[BOOT:WIRED] MCPService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MCPService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] MCPService disabled by profile")

    # --- MountManager: VFS-backed mount config persistence ---
    # Constructed here (post-kernel tier) because the underlying
    # MetastoreMountStore writes through public VFS syscalls and needs
    # a live NexusFS handle. Pre-kernel tier (factory/_system.py) used
    # to instantiate this against the metastore directly — that was the
    # ABC leak we eliminated.
    try:
        from nexus.bricks.mount.metastore_mount_store import MetastoreMountStore
        from nexus.bricks.mount.mount_manager import MountManager

        _mount_store = MetastoreMountStore(nx)
        services["mount_manager"] = MountManager(_mount_store)
        logger.debug("[BOOT:WIRED] MountManager created (VFS-backed)")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] MountManager unavailable: %s", exc)

    # --- ReBAC namespace + version stores: VFS-backed ---
    # Same story as MountManager — the stores write through public VFS
    # syscalls so they can only be constructed after NexusFS exists.
    # ReBACManager (built in tier 1) had ``namespace_store=None`` /
    # ``version_store=None``; we patch them in here. The manager only
    # consumes these lazily on first permission/namespace operation,
    # so this late binding is safe.
    rebac_manager = services.get("rebac_manager")
    if rebac_manager is not None:
        try:
            from nexus.bricks.rebac.consistency.metastore_namespace_store import (
                MetastoreNamespaceStore,
            )
            from nexus.bricks.rebac.consistency.metastore_version_store import (
                MetastoreVersionStore,
            )

            rebac_manager._namespace_store = MetastoreNamespaceStore(nx)
            rebac_manager._version_store = MetastoreVersionStore(nx)
            logger.debug("[BOOT:WIRED] ReBAC namespace + version stores wired (VFS-backed)")
        except Exception as exc:
            logger.warning("[BOOT:WIRED] ReBAC namespace/version stores wiring failed: %s", exc)

    # --- MountPersistService: Mount persistence ---
    # Created with mount_service=None initially; wired after MountService creation below.
    mount_persist_service: Any = None
    if gateway is not None:
        try:
            from nexus.bricks.mount.mount_persist_service import MountPersistService

            mount_persist_service = MountPersistService(
                mount_manager=services.get("mount_manager"),
                mount_service=None,  # wired after MountService creation below
            )
            logger.debug("[BOOT:WIRED] MountPersistService created (mount_service pending)")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MountPersistService unavailable: %s", exc)

    # --- MountService: Dynamic backend mounting operations ---
    # Moved after sub-services so DI deps are available (Issue #636).
    mount_service: Any = None
    try:
        from nexus.bricks.mount.mount_service import MountService

        mount_service = MountService(
            dlc=nx._driver_coordinator,
            mount_manager=services.get("mount_manager"),
            nexus_fs=nx,
            gateway=gateway,
            mount_persist_service=mount_persist_service,
            oauth_service=oauth_service,
        )
        logger.debug("[BOOT:WIRED] MountService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] MountService unavailable: %s", exc)

    # Wire MountPersistService -> MountService (break circular dep)
    if mount_persist_service is not None and mount_service is not None:
        mount_persist_service._mounts_ref = mount_service

    # --- SearchService: list/glob/grep are kernel-level VFS operations (Issue #2194) ---
    # Always create SearchService regardless of "search" brick — basic directory
    # listing, glob matching, and grep must work even in KERNEL-only mode.
    # The "search" brick gates advanced features (semantic search, indexing),
    # not core filesystem enumeration.
    search_service: Any = None
    try:
        import os as _os

        from nexus.bricks.search.search_service import SearchService

        # Issue #3778: thread the active deployment profile so semantic_search
        # can detect SANDBOX and route to the BM25S fallback with a stamped
        # ``semantic_degraded=True`` flag.  Profile is sourced from env (set
        # by connect()/CLI); falls back to None for callers that don't set it.
        # ``nx._config`` isn't yet attached at this point in the boot — the
        # env var is the canonical signal here.
        _profile = (_os.environ.get("NEXUS_PROFILE") or "").strip().lower() or None

        # Issue #3778: optional local sqlite-vec backend (SANDBOX only).
        # We only attempt the import when:
        #   * profile == "sandbox"  AND
        #   * enable_vector_search is True  (opt-in; default False on SANDBOX)
        #
        # The user can opt-in via either ``cfg.enable_vector_search=True`` in
        # the config dict (preferred) or ``NEXUS_ENABLE_VECTOR_SEARCH=true``
        # in the env. Both ``sqlite-vec`` and ``litellm`` must be importable;
        # missing either degrades silently to the federation/BM25S chain.
        #
        # Note: ``nx._config`` is only attached AFTER ``create_nexus_fs()``
        # returns (see nexus/__init__.py), so at this point in the boot we
        # rely on env-var signalling. ``connect()`` propagates the config
        # dict's ``enable_vector_search`` to ``NEXUS_ENABLE_VECTOR_SEARCH``
        # before invoking the factory (Issue #3778).
        _sqlite_vec_backend: Any = None

        def _env_truthy(name: str) -> bool:
            return (_os.environ.get(name) or "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

        _enable_vec = _env_truthy("NEXUS_ENABLE_VECTOR_SEARCH")
        if _profile == "sandbox" and _enable_vec:
            try:
                from nexus.bricks.search.sqlite_vec_backend import SqliteVecBackend

                # Derive db path. Prefer NEXUS_DB_PATH; otherwise pull from
                # the record_store's SQLAlchemy engine URL (only valid when
                # the record store is SQLite-backed, which is the SANDBOX
                # case by construction).
                _vec_db_path: str | None = _os.environ.get("NEXUS_DB_PATH") or None
                if not _vec_db_path:
                    _rs = getattr(nx, "_record_store", None)
                    _eng = getattr(_rs, "engine", None) if _rs is not None else None
                    if _eng is not None:
                        _url = str(_eng.url)
                        # SQLAlchemy SQLite URL: sqlite:////absolute/path.db
                        if _url.startswith("sqlite:///"):
                            _vec_db_path = _url[len("sqlite:///") :]
                            # Restore leading slash for absolute paths
                            # (SQLAlchemy uses 4 slashes: sqlite:////abs).
                            if _url.startswith("sqlite:////"):
                                _vec_db_path = "/" + _vec_db_path.lstrip("/")

                if _vec_db_path:
                    _sqlite_vec_backend = SqliteVecBackend(db_path=str(_vec_db_path))
                    logger.info(
                        "[BOOT:WIRED] SqliteVecBackend created (db=%s) — SANDBOX local vector search enabled",
                        _vec_db_path,
                    )
                else:
                    logger.warning(
                        "[BOOT:WIRED] SANDBOX enable_vector_search=true but no db_path resolved; "
                        "skipping local vector backend"
                    )
            except ImportError as exc:
                logger.warning(
                    "[BOOT:WIRED] SANDBOX enable_vector_search=true but optional dep missing (%s); "
                    "install with: pip install 'nexus-ai-fs[sandbox]' — falling back to federation/BM25S",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "[BOOT:WIRED] SqliteVecBackend init failed: %s — falling back to federation/BM25S",
                    exc,
                )

        # Issue #3778 (R2 review): look up an already-constructed federation
        # dispatcher on the ServiceRegistry / NexusFS if one is available, so
        # the SANDBOX semantic path actually dispatches when the deployment
        # wired federation in from outside (non-default, but not impossible).
        # The canonical construction site for dispatchers is the HTTP router
        # (server/api/v2/routers/search.py); factory-level boot does not
        # build one. For true SANDBOX (single-process, no peers) this stays
        # None and the semantic path falls through the "no-peers" synth
        # FederatedSearchResponse → BM25S degradation, which is correct.
        _federation_dispatcher = getattr(nx, "_federation_dispatcher", None) or services.get(
            "federation_dispatcher"
        )

        search_service = SearchService(
            metadata_store=nx.metadata,
            permission_enforcer=services.get("permission_enforcer"),
            dlc=nx._driver_coordinator,
            rebac_manager=services.get("rebac_manager"),
            enforce_permissions=nx._perm_config.enforce,
            default_context=nx._init_cred,
            record_store=getattr(nx, "_record_store", None),
            gateway=gateway,
            deployment_profile=_profile,
            sqlite_vec_backend=_sqlite_vec_backend,
            federation_dispatcher=_federation_dispatcher,
        )
        logger.debug(
            "[BOOT:WIRED] SearchService created (kernel-level, profile=%s, sqlite_vec=%s)",
            _profile,
            _sqlite_vec_backend is not None,
        )
    except Exception as exc:
        logger.warning(
            "[BOOT:WIRED] SearchService unavailable (glob/grep will not work): %s",
            exc,
            exc_info=True,
        )

    # Wire SearchService -> MountService for post-mount indexing (Issue #3148)
    if mount_service is not None and search_service is not None:
        mount_service._search_service = search_service

    # Wire MountService -> WorkflowServices for scheduled sync (Issue #3148)
    if mount_service is not None:
        workflow_engine = getattr(nx, "workflow_engine", None)
        if workflow_engine is not None:
            _wf_services = getattr(workflow_engine, "_services", None)
            if _wf_services is not None and hasattr(_wf_services, "mount_sync"):
                _wf_services.mount_sync = mount_service
                logger.debug("[BOOT:WIRED] MountService -> WorkflowServices.mount_sync")

    # --- ShareLinkService: Share link operations ---
    share_link_service: Any = None
    if _on("discovery"):
        try:
            from nexus.bricks.share_link.share_link_service import ShareLinkService

            share_link_service = ShareLinkService(
                gateway=gateway,
                enforce_permissions=nx._perm_config.enforce,
            )
            logger.debug("[BOOT:WIRED] ShareLinkService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] ShareLinkService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] ShareLinkService disabled by profile")

    # --- RPC / helper services (Issue #2133) ---
    # Pre-extract optional NexusFS attrs to avoid mypy getattr+None inference issues
    _nx_init_cred: Any = nx._init_cred
    _nx_session_factory: Any = getattr(nx, "SessionLocal", None)
    workspace_rpc_service: Any = None
    try:
        from nexus.services.workspace.workspace_rpc_service import WorkspaceRPCService

        workspace_rpc_service = WorkspaceRPCService(
            workspace_manager=services["workspace_manager"],
            workspace_registry=services["workspace_registry"],
            vfs=nx,
            default_context=_nx_init_cred,
            snapshot_service=services.get("snapshot_service"),
        )
        logger.debug("[BOOT:WIRED] WorkspaceRPCService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] WorkspaceRPCService unavailable: %s", exc)

    agent_rpc_service: Any = None
    try:
        agent_warmup_service: Any = None
        _agent_registry = getattr(nx, "_agent_registry", None)
        if _agent_registry is not None:
            try:
                from nexus.services.agents.agent_warmup import AgentWarmupService
                from nexus.services.agents.warmup_steps import register_standard_steps

                agent_warmup_service = AgentWarmupService(
                    agent_registry=_agent_registry,
                    namespace_manager=services.get("async_namespace_manager"),
                    enabled_bricks=services.get("enabled_bricks", frozenset()),
                    cache_store=getattr(services.get("cache_brick"), "cache_store", None),
                    mcp_config=None,
                )
                register_standard_steps(agent_warmup_service)
            except Exception as exc:
                logger.warning("[BOOT:WIRED] AgentWarmupService unavailable: %s", exc)

        from nexus.services.agents.agent_rpc_service import AgentRPCService

        agent_rpc_service = AgentRPCService(
            vfs=nx,
            metastore=nx.metadata,
            session_factory=_nx_session_factory,
            record_store=nx._record_store,
            entity_registry=services.get("entity_registry"),
            rebac_manager=services.get("rebac_manager"),
            wallet_provisioner=services.get("wallet_provisioner"),
            api_key_creator=services.get("api_key_creator"),
            key_service=getattr(nx, "_key_service", None),
            rmdir_fn=nx.rmdir if hasattr(nx, "rmdir") else None,
            rebac_create_fn=(rebac_service.rebac_create_sync if rebac_service else None),
            rebac_list_tuples_fn=(rebac_service.rebac_list_tuples_sync if rebac_service else None),
            rebac_delete_fn=(rebac_service.rebac_delete_sync if rebac_service else None),
            agent_warmup_service=agent_warmup_service,
        )
        logger.debug("[BOOT:WIRED] AgentRPCService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] AgentRPCService unavailable: %s", exc)

    # AgentStatusResolver moved to orchestrator._register_vfs_hooks() (Issue #1570, #1810)

    # --- AgentRegistry + AcpService + EvictionManager (Issue #1792) ---
    # AgentRegistry is constructed by the first consumer that needs it.
    # No-agent profiles (REMOTE) skip this entire block.
    _agent_reg: Any = None
    _acp_ref = nx.service("agent_registry")
    if _acp_ref is not None:
        _agent_reg = _acp_ref
    if _agent_reg is None:
        try:
            from nexus.services.agents.agent_registry import AgentRegistry

            _agent_reg = AgentRegistry()
            nx.sys_setattr("/__sys__/services/agent_registry", service=_agent_reg)
            logger.debug("[BOOT:WIRED] AgentRegistry constructed by wired tier")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] AgentRegistry unavailable: %s", exc)

    # Late-bind AgentRegistry → AgentRPCService (Issue #3524)
    if _agent_reg is not None and agent_rpc_service is not None:
        agent_rpc_service._agent_registry = _agent_reg
        if getattr(agent_rpc_service, "_agent_warmup_service", None) is None:
            try:
                from nexus.services.agents.agent_warmup import AgentWarmupService
                from nexus.services.agents.warmup_steps import register_standard_steps

                agent_warmup_service = AgentWarmupService(
                    agent_registry=_agent_reg,
                    namespace_manager=services.get("async_namespace_manager"),
                    enabled_bricks=services.get("enabled_bricks", frozenset()),
                    cache_store=getattr(services.get("cache_brick"), "cache_store", None),
                    mcp_config=None,
                )
                register_standard_steps(agent_warmup_service)
                agent_rpc_service._agent_warmup_service = agent_warmup_service
            except Exception as exc:
                logger.warning("[BOOT:WIRED] Late AgentWarmupService unavailable: %s", exc)

    # EvictionManager (QoS-aware agent eviction)
    if _agent_reg is not None:
        try:
            from nexus.contracts.deployment_profile import DeploymentProfile as _DP
            from nexus.lib.performance_tuning import resolve_profile_tuning
            from nexus.services.agents.eviction_manager import EvictionManager
            from nexus.services.agents.eviction_policy import QoSEvictionPolicy
            from nexus.services.agents.resource_monitor import ResourceMonitor

            _profile_tuning = resolve_profile_tuning(_DP.FULL)
            _eviction_tuning = _profile_tuning.eviction
            _eviction_manager = EvictionManager(
                agent_registry=_agent_reg,
                monitor=ResourceMonitor(tuning=_eviction_tuning),
                policy=QoSEvictionPolicy(),
                tuning=_eviction_tuning,
            )
            nx.sys_setattr("/__sys__/services/eviction_manager", service=_eviction_manager)
            logger.debug("[BOOT:WIRED] EvictionManager created (QoS-aware)")
        except Exception as exc:
            logger.warning("[BOOT:WIRED] EvictionManager unavailable: %s", exc)

    # AcpService (agent call protocol)
    acp_rpc_service: Any = None
    _acp_service: Any = None
    if _agent_reg is not None:
        try:
            from nexus.services.acp.service import AcpService

            _acp_service = AcpService(
                agent_registry=_agent_reg,
                zone_id=ROOT_ZONE_ID,
            )
            nx.sys_setattr("/__sys__/services/acp_service", service=_acp_service)
            logger.debug("[BOOT:WIRED] AcpService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] AcpService unavailable: %s", exc)
    if _acp_service is not None:
        # Late-bind NexusFS for VFS-routed file I/O (``everything is a file``).
        if hasattr(_acp_service, "bind_fs"):
            _acp_service.bind_fs(nx)
        # PipeManager deleted — agent stdio pipes registered via NexusFS._kernel directly.
        # Wire agent termination → permission lease revocation (Issue #3398 decision 2A)
        _perm_lease_table = getattr(nx, "_permission_lease_table", None)
        if _perm_lease_table is not None and hasattr(_acp_service, "register_on_terminate"):
            _acp_service.register_on_terminate(
                "perm-lease-revoke", _perm_lease_table.invalidate_agent
            )
        try:
            from nexus.services.acp.acp_rpc_service import AcpRPCService

            acp_rpc_service = AcpRPCService(acp_service=_acp_service)
            logger.debug("[BOOT:WIRED] AcpRPCService created")
        except Exception as exc:
            logger.warning("[BOOT:WIRED] AcpRPCService unavailable: %s", exc)

    user_provisioning_service: Any = None
    try:
        from nexus.services.lifecycle.user_provisioning import UserProvisioningService

        user_provisioning_service = UserProvisioningService(
            vfs=nx,
            session_factory=_nx_session_factory,
            entity_registry=services.get("entity_registry"),
            api_key_creator=services.get("api_key_creator"),
            backend=_root_backend,
            rebac_manager=services.get("rebac_manager"),
            rmdir_fn=nx.rmdir if hasattr(nx, "rmdir") else None,
            rebac_create_fn=(rebac_service.rebac_create_sync if rebac_service else None),
            rebac_delete_fn=(rebac_service.rebac_delete_sync if rebac_service else None),
            register_workspace_fn=(
                workspace_rpc_service.register_workspace if workspace_rpc_service else None
            ),
            register_agent_fn=(agent_rpc_service.register_agent if agent_rpc_service else None),
            list_cache=getattr(nx, "_list_cache", None),
            exists_cache=getattr(nx, "_exists_cache", None),
        )
        logger.debug("[BOOT:WIRED] UserProvisioningService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] UserProvisioningService unavailable: %s", exc)

    sandbox_rpc_service: Any = None
    if _on("sandbox"):
        try:
            from nexus.sandbox.sandbox_rpc_service import SandboxRPCService

            sandbox_rpc_service = SandboxRPCService(
                session_factory=_nx_session_factory,
                default_context=_nx_init_cred,
                config=nx._config,
            )
            logger.debug("[BOOT:WIRED] SandboxRPCService created")
        except ImportError:
            logger.debug("[BOOT:WIRED] SandboxRPCService not installed")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SandboxRPCService unavailable: %s", exc)

    # --- MetadataExportService: JSONL metadata export/import (Issue #662) ---
    metadata_export_service: Any = None
    try:
        from nexus.factory._metadata_export import create_metadata_export_service

        metadata_export_service = create_metadata_export_service(nx)
    except Exception as exc:
        logger.debug("[BOOT:WIRED] MetadataExportService unavailable: %s", exc)

    descendant_checker: Any = None
    try:
        from nexus.services.namespace.descendant_access import DescendantAccessChecker

        _rebac_for_dc = services.get("rebac_manager")
        descendant_checker = DescendantAccessChecker(
            rebac_manager=_rebac_for_dc,
            rebac_service=rebac_service,
            dir_visibility_cache=getattr(_rebac_for_dc, "dir_visibility_cache", None)
            if _rebac_for_dc
            else None,
            permission_enforcer=services.get("permission_enforcer"),
            metadata_store=nx.metadata,
        )
        logger.debug("[BOOT:WIRED] DescendantAccessChecker created")
    except Exception as exc:
        logger.debug("[BOOT:WIRED] DescendantAccessChecker unavailable: %s", exc)

    # --- TimeTravelService: historical operation-point queries (Issue #882) ---
    time_travel_service: Any = None
    if _nx_session_factory is not None:
        try:
            from nexus.bricks.versioning.time_travel_service import TimeTravelService

            time_travel_service = TimeTravelService(
                session_factory=_nx_session_factory,
                backend=_root_backend,
                default_zone_id=getattr(_nx_init_cred, "zone_id", None),
            )
            logger.debug("[BOOT:WIRED] TimeTravelService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] TimeTravelService unavailable: %s", exc)

    # --- OperationsService: audit trail queries + undo (Issue #882) ---
    operations_service: Any = None
    if _nx_session_factory is not None:
        try:
            from nexus.bricks.versioning.operation_undo_service import OperationUndoService
            from nexus.bricks.versioning.operations_service import OperationsService

            _undo_service = OperationUndoService(
                dlc=nx._driver_coordinator,
                write_fn=nx.sys_write,
                delete_fn=nx.sys_unlink,
                rename_fn=nx.sys_rename,
                exists_fn=nx.access,
                fallback_backend=getattr(nx, "backend", None),
            )
            operations_service = OperationsService(
                session_factory=_nx_session_factory,
                undo_service=_undo_service,
            )
            logger.debug("[BOOT:WIRED] OperationsService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] OperationsService unavailable: %s", exc)

    result: dict[str, Any] = {
        "rebac_service": rebac_service,
        "mount_service": mount_service,
        "gateway": gateway,
        "mount_persist_service": mount_persist_service,
        "mcp_service": mcp_service,
        "oauth_service": oauth_service,
        "search_service": search_service,
        "share_link_service": share_link_service,
        "time_travel_service": time_travel_service,
        "operations_service": operations_service,
        "workspace_rpc_service": workspace_rpc_service,
        "agent_rpc_service": agent_rpc_service,
        "acp_rpc_service": acp_rpc_service,
        "user_provisioning_service": user_provisioning_service,
        "sandbox_rpc_service": sandbox_rpc_service,
        "metadata_export_service": metadata_export_service,
        "descendant_checker": descendant_checker,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info(
        "[BOOT:WIRED] %d/%d services ready (%.3fs)",
        active,
        len(result),
        elapsed,
    )
    return result


def _initialize_wired_ipc(nx: Any, services: dict[str, Any]) -> None:
    """IPC mount + provisioner creation — deferred from link() to initialize().

    Performs I/O (mkdir) so it cannot run in the pure-memory link() phase.
    NexusFS is passed directly to IPC components (no adapter layer).
    """
    _ipc_zone_id = services.get("ipc_zone_id")
    if _ipc_zone_id is None:
        # If the IPC brick isn't registered at all, the profile has disabled
        # it — skip IPC init entirely (fail-closed). Only fall back when the
        # brick DID register the zone but the value was lost on its way into
        # the services dict (the upstream wiring bug this patch addresses).
        _svc_fn = getattr(nx, "service", None)
        _registered = _svc_fn("ipc_zone_id") if _svc_fn is not None else None
        if _registered is None:
            return
        # Use the actual registered value — service_lookup returns raw instance.
        _resolved = _registered
        if not isinstance(_resolved, str) or not _resolved:
            logger.error(
                "[BOOT:WIRED] IPC init: registered ipc_zone_id has invalid type/"
                "value (%r); skipping IPC wiring to avoid cross-zone leak.",
                _resolved,
            )
            return
        _ipc_zone_id = _resolved
        logger.warning(
            "[BOOT:WIRED] IPC init: ipc_zone_id registered but not threaded "
            "through services dict — recovered from service registry (%r). "
            "Caller in _lifecycle.py should thread the value explicitly.",
            _ipc_zone_id,
        )

    try:
        # Mount a LocalConnector at /agents for IPC file storage
        from pathlib import Path

        from nexus.backends.storage.local_connector import LocalConnectorBackend

        _ipc_data_dir = Path(getattr(nx, "_data_dir", "data")) / "ipc"
        _ipc_data_dir.mkdir(parents=True, exist_ok=True)
        _ipc_connector = LocalConnectorBackend(local_path=_ipc_data_dir)
        from nexus.contracts.metadata import DT_MOUNT

        nx.sys_setattr("/agents", entry_type=DT_MOUNT, backend=_ipc_connector)

        # Ensure the /agents metadata entry has target_zone_id set.
        # Single source of truth = _ipc_zone_id. Using nx._zone_id here would
        # make mount metadata disagree with AgentProvisioner in federated /
        # multi-zone setups where the two can legitimately diverge.
        # Reconcile on mismatch — legacy deployments may have persisted a
        # stale value (e.g. nx._zone_id) from before this patch.
        try:
            from nexus.contracts.metadata import DT_MOUNT

            existing = nx.metadata.get("/agents")
            if existing is not None and existing.target_zone_id != _ipc_zone_id:
                from dataclasses import replace as _replace

                _prior = existing.target_zone_id
                # DT_REG/DT_DIR/DT_MOUNT/DT_PIPE are enum values (0..3), not
                # bitflags — ``DT_DIR | DT_MOUNT`` would produce DT_PIPE (=3)
                # and corrupt the /agents inode.
                updated = _replace(
                    existing,
                    entry_type=DT_MOUNT,
                    target_zone_id=_ipc_zone_id,
                )
                nx.metadata.put(updated)
                if _prior:
                    logger.warning(
                        "[BOOT:WIRED] /agents mount target_zone_id reconciled: "
                        "%r → %r (matches AgentProvisioner zone)",
                        _prior,
                        _ipc_zone_id,
                    )
                else:
                    logger.debug(
                        "[BOOT:WIRED] Set target_zone_id=%s on /agents mount", _ipc_zone_id
                    )
        except Exception as e:
            logger.debug("[BOOT:WIRED] Could not set /agents target_zone_id: %s", e)

        # Create AgentProvisioner with NexusFS directly (no adapter)
        from nexus.bricks.ipc.provisioning import AgentProvisioner

        _provisioner = AgentProvisioner(
            vfs=nx,
            zone_id=_ipc_zone_id,
        )
        services["ipc_provisioner"] = _provisioner

        # Also register with the service registry so lifespan/ipc.py can
        # resolve it via nx.service("ipc_provisioner"). Without this the
        # provisioner only lives in the local services dict and the FastAPI
        # lifespan hook never wires it onto app.state.ipc_nexus_fs.
        # Use enlist(allow_overwrite=True) so re-entry on hot-reload replaces atomically.
        nx.sys_setattr(
            "/__sys__/services/ipc_provisioner",
            service=_provisioner,
            allow_overwrite=True,
        )

        # Wire provisioner into AgentRegistry so register → provision is automatic
        _agent_reg_svc = nx.service("agent_registry")
        _agent_reg = _agent_reg_svc if _agent_reg_svc is not None else None
        if _agent_reg is not None and hasattr(_agent_reg, "set_provisioner"):
            _agent_reg.set_provisioner(_provisioner)

        logger.debug(
            "[BOOT:WIRED] IPC LocalConnector mounted at /agents + AgentProvisioner created"
        )
    except Exception as exc:
        logger.warning("[BOOT:WIRED] IPC mount failed: %s", exc)


# Backward compatibility alias
_boot_wired_services = _boot_post_kernel_services
