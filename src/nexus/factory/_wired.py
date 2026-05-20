"""Boot Tier 2b (POST-KERNEL) — services needing NexusFS reference."""

import logging
import time
from collections.abc import Callable
from typing import Any, cast

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

    # --- IPC brick deleted (Phase M of parallel-layers PR; PR #3912's
    #     Rust replacement covers the wakeup / discovery surface).

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
    if nx is not None:
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

        # Issue #3778: local sqlite-vec backend (SANDBOX profile).
        #
        # On SANDBOX: vector search is ON by default — the [sandbox] extra
        # bundles ``sqlite-vec`` + ``fastembed`` so the offline path works
        # out of the box. Users can opt out with
        # ``NEXUS_DISABLE_VECTOR_SEARCH=1`` (e.g. to skip the ~30 MB ONNX
        # model download). On other profiles vector search remains opt-in
        # via ``NEXUS_ENABLE_VECTOR_SEARCH=1``.
        #
        # If both ``sqlite-vec`` and an embedder (fastembed OR a remote
        # API key for litellm) are reachable, we wire the backend.
        # Missing pieces degrade silently (with a clear WARNING) to the
        # federation/BM25S chain.
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

        def _env_falsy(name: str) -> bool:
            return (_os.environ.get(name) or "").strip().lower() in (
                "0",
                "false",
                "no",
                "off",
            )

        if _profile == "sandbox":
            # SANDBOX: vector search ON by default. Two opt-outs honored,
            # in priority order:
            #   1. ``NEXUS_DISABLE_VECTOR_SEARCH=1`` — new explicit knob.
            #   2. ``NEXUS_ENABLE_VECTOR_SEARCH=false`` — preserves the
            #      legacy ``config.enable_vector_search=False`` opt-out
            #      that ``connect()`` propagates to this env var. Without
            #      this branch, deployments that previously turned vec
            #      OFF via config would silently get it back on after the
            #      default flip (Codex review, high).
            if _env_truthy("NEXUS_DISABLE_VECTOR_SEARCH") or _env_falsy(
                "NEXUS_ENABLE_VECTOR_SEARCH"
            ):
                _enable_vec = False
            else:
                _enable_vec = True
        else:
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
                    "[BOOT:WIRED] SANDBOX vector search disabled — optional dep missing (%s). "
                    "Install with: pip install 'nexus-ai-fs[sandbox]' (bundles sqlite-vec + "
                    "fastembed for offline embeddings). Falling back to keyword-only search.",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "[BOOT:WIRED] SqliteVecBackend init failed: %s — "
                    "falling back to keyword-only search.",
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
            metadata_store=nx._kernel,
            permission_enforcer=services.get("permission_enforcer"),
            dlc=nx._driver_coordinator,
            rebac_manager=services.get("rebac_manager"),
            enforce_permissions=nx._perm_config.enforce,
            default_context=nx._init_cred,
            record_store=getattr(nx, "_record_store", None),
            nexus_fs=nx,
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
                nexus_fs=nx,
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

    # --- Workspace Manager (moved from _system.py — needs sys_readdir §2.5) ---
    workspace_manager: Any = None
    try:
        from nexus.contracts.protocols.rebac import ReBACBrickProtocol
        from nexus.services.workspace.workspace_manager import WorkspaceManager

        _ws_zone_id = getattr(_nx_init_cred, "zone_id", None)
        _ws_agent_id = getattr(_nx_init_cred, "agent_id", None)
        workspace_manager = WorkspaceManager(
            nexus_fs=nx,
            rebac_manager=cast(ReBACBrickProtocol, services.get("rebac_manager")),
            zone_id=_ws_zone_id,
            agent_id=_ws_agent_id,
            record_store=getattr(nx, "_record_store", None),
        )
        services["workspace_manager"] = workspace_manager
        logger.debug("[BOOT:WIRED] WorkspaceManager created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] WorkspaceManager unavailable: %s", exc)

    # --- Context Branch Service (moved from _system.py — depends on
    #     workspace_manager which is now wired here) ---
    context_branch_service: Any = None
    try:
        from nexus.contracts.protocols.rebac import ReBACBrickProtocol
        from nexus.services.workspace.context_branch import ContextBranchService

        context_branch_service = ContextBranchService(
            workspace_manager=workspace_manager,
            record_store=nx._record_store,
            rebac_manager=cast(ReBACBrickProtocol, services.get("rebac_manager")),
            default_zone_id=getattr(_nx_init_cred, "zone_id", None),
            default_agent_id=getattr(_nx_init_cred, "agent_id", None),
        )
        services["context_branch_service"] = context_branch_service
        logger.debug("[BOOT:WIRED] ContextBranchService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] ContextBranchService unavailable: %s", exc)

    # --- Tiger Cache Manager (moved from _system.py — initialize()'s
    #     resource-map sync lists via NexusFS.sys_readdir) ---
    try:
        from nexus.bricks.rebac.tiger_cache_manager import TigerCacheManager
        from nexus.contracts.constants import ROOT_ZONE_ID

        _tiger_cache_manager = TigerCacheManager(
            rebac_manager=services.get("rebac_manager"),
            nexus_fs=nx,
            default_zone_id=getattr(_nx_init_cred, "zone_id", None) or ROOT_ZONE_ID,
        )
        _tiger_cache_manager.initialize()
        logger.debug("[BOOT:WIRED] TigerCacheManager created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] TigerCacheManager unavailable: %s", exc)

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
        # AgentRegistry is the kernel SSOT — fetch it via the kernel
        # handle. None for no-agent profiles (REMOTE). Use getattr so a stale
        # PyKernel without the `agent_registry` getter (Issue #4017) degrades
        # to an AgentRPCService without warmup, instead of taking down the
        # whole try block and dropping AgentRPCService entirely.
        _kernel_for_warmup = getattr(nx, "_kernel", None)
        _agent_registry = getattr(_kernel_for_warmup, "agent_registry", None)
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
            metastore=nx._kernel,
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
    # AgentRegistry is the kernel SSOT — Python callers reach it through
    # the `agent_registry` getter on the Rust kernel handle. No-agent
    # profiles (REMOTE) skip this block (no kernel wiring).
    _agent_reg: Any = None
    _kernel_for_reg = getattr(nx, "_kernel", None)
    if _kernel_for_reg is not None:
        try:
            _agent_reg = _kernel_for_reg.agent_registry
            logger.debug("[BOOT:WIRED] AgentRegistry handle obtained from kernel SSOT")
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

    # AcpService + ManagedAgentService installation is now managed internally
    # by the nexus-cluster process. The Rust kernel installs these services
    # during its own boot sequence — no Python-side PyO3 calls needed.
    if _agent_reg is not None:
        logger.debug(
            "[BOOT:WIRED] AcpService + ManagedAgentService managed internally by nexus-cluster"
        )

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
            nexus_fs=nx,
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
                nexus_fs=nx,
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
        "mount_persist_service": mount_persist_service,
        "mcp_service": mcp_service,
        "oauth_service": oauth_service,
        "search_service": search_service,
        "share_link_service": share_link_service,
        "time_travel_service": time_travel_service,
        "operations_service": operations_service,
        "workspace_rpc_service": workspace_rpc_service,
        "agent_rpc_service": agent_rpc_service,
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


# Phase M deleted `_initialize_wired_ipc` along with the entire
# `nexus.bricks.ipc` brick — its Rust replacement is in flight under
# PR #3912 and the LocalConnector / AgentProvisioner wiring it set up
# is unused on every supported profile.


# Backward compatibility alias
_boot_wired_services = _boot_post_kernel_services
