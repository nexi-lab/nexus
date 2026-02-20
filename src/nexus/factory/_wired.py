"""Boot Tier 2b (WIRED) — services needing NexusFS reference."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _boot_wired_services(
    nx: Any,
    kernel_services: Any,
    system_services: Any,
    brick_services: Any,
    brick_on: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """Boot Tier 2b (WIRED) — services needing NexusFS reference.

    Two-phase init: called AFTER NexusFS construction in ``create_nexus_fs()``.
    ``NexusFSGateway`` breaks the circular dependency between kernel and services.

    Profile gating is applied via ``brick_on`` — same callback used by other tiers.
    Services that fail to construct are set to None (degraded mode).

    Issue #643: Migrated from ``NexusFS._wire_services()`` to factory.py
    so the kernel never imports or creates services.

    Args:
        nx: The NexusFS instance (already constructed).
        kernel_services: KernelServices container (Tier 0 — router only).
        system_services: SystemServices container (Tier 1 — rebac, permissions, etc.).
        brick_services: BrickServices container (Tier 2).
        brick_on: Callable ``(name: str) -> bool`` for profile-based gating.

    Returns:
        Dict of service name -> instance (some may be None).
    """
    from nexus.factory._helpers import _make_gate

    t0 = time.perf_counter()
    _on = _make_gate(brick_on)

    # --- NexusFSGateway: adapter breaking circular dep (Issue #1287) ---
    gateway: Any = None
    try:
        from nexus.services.gateway import NexusFSGateway

        gateway = NexusFSGateway(nx)
        logger.debug("[BOOT:WIRED] NexusFSGateway created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] NexusFSGateway unavailable: %s", exc)

    # --- ReBACService: Permission and access control operations ---
    rebac_service: Any = None
    try:
        from nexus.services.rebac.rebac_service import ReBACService

        rebac_service = ReBACService(
            rebac_manager=system_services.rebac_manager,
            enforce_permissions=getattr(nx, "_enforce_permissions", True),
            enable_audit_logging=True,
            circuit_breaker=brick_services.rebac_circuit_breaker,
            file_reader=lambda path: nx.read(path),
            permission_enforcer=getattr(nx, "_permission_enforcer", None),
        )
        logger.debug("[BOOT:WIRED] ReBACService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] ReBACService unavailable: %s", exc)

    # --- MCPService: Model Context Protocol operations ---
    mcp_service: Any = None
    if _on("mcp"):
        try:
            from nexus.services.mcp.mcp_service import MCPService

            mcp_service = MCPService(filesystem=nx)
            logger.debug("[BOOT:WIRED] MCPService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MCPService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] MCPService disabled by profile")

    # --- LLMService + LLMSubsystem: LLM integration ---
    llm_service: Any = None
    llm_subsystem: Any = None
    if _on("llm"):
        try:
            from nexus.services.llm.llm_service import LLMService

            llm_service = LLMService(nexus_fs=nx)

            from nexus.services.subsystems.llm_subsystem import LLMSubsystem

            llm_subsystem = LLMSubsystem(llm_service=llm_service)
            logger.debug("[BOOT:WIRED] LLMService + LLMSubsystem created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] LLMService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] LLMService disabled by profile")

    # --- OAuthService: OAuth authentication operations ---
    oauth_service: Any = None
    if _on("sandbox"):
        try:
            import os

            from nexus.services.oauth.oauth_service import OAuthService

            oauth_service = OAuthService(
                oauth_factory=None,
                token_manager=None,
                filesystem=nx,
                database_url=os.getenv("TOKEN_MANAGER_DB"),
                oauth_config=getattr(getattr(nx, "_config", None), "oauth", None),
                mount_lister=lambda: [
                    (m.mount_point, type(m.backend).__name__)
                    for m in kernel_services.router.list_mounts()
                ],
            )
            logger.debug("[BOOT:WIRED] OAuthService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] OAuthService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] OAuthService disabled by profile")

    # --- MountCoreService: Internal mount operations (gateway-dependent) ---
    mount_core_service: Any = None
    if gateway is not None:
        try:
            from nexus.services.mount.mount_core_service import MountCoreService

            mount_core_service = MountCoreService(gateway)
            logger.debug("[BOOT:WIRED] MountCoreService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MountCoreService unavailable: %s", exc)

    # --- SyncService: Sync operations (gateway-dependent) ---
    sync_service: Any = None
    if gateway is not None:
        try:
            from nexus.system_services.sync.sync_service import SyncService

            sync_service = SyncService(gateway)
            logger.debug("[BOOT:WIRED] SyncService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SyncService unavailable: %s", exc)

    # --- SyncJobService: Sync job management ---
    sync_job_service: Any = None
    if gateway is not None and sync_service is not None:
        try:
            from nexus.system_services.sync.sync_job_service import SyncJobService

            sync_job_service = SyncJobService(gateway, sync_service)
            logger.debug("[BOOT:WIRED] SyncJobService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SyncJobService unavailable: %s", exc)

    # --- MountPersistService: Mount persistence ---
    mount_persist_service: Any = None
    if mount_core_service is not None:
        try:
            from nexus.services.mount.mount_persist_service import MountPersistService

            mount_persist_service = MountPersistService(
                mount_manager=system_services.mount_manager,
                mount_service=mount_core_service,
                sync_service=sync_service,
            )
            logger.debug("[BOOT:WIRED] MountPersistService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] MountPersistService unavailable: %s", exc)

    # --- MountService: Dynamic backend mounting operations ---
    # Moved after sub-services so DI deps are available (Issue #636).
    mount_service: Any = None
    try:
        from nexus.services.mount.mount_service import MountService

        mount_service = MountService(
            router=kernel_services.router,
            mount_manager=system_services.mount_manager,
            nexus_fs=nx,
            sync_service=sync_service,
            sync_job_service=sync_job_service,
            mount_core_service=mount_core_service,
            mount_persist_service=mount_persist_service,
            oauth_service=oauth_service,
        )
        logger.debug("[BOOT:WIRED] MountService created")
    except Exception as exc:
        logger.warning("[BOOT:WIRED] MountService unavailable: %s", exc)

    # --- SkillService: Skill management (Issue #2035) ---
    skill_service: Any = brick_services.skill_service
    if skill_service is None and _on("skills") and gateway is not None:
        try:
            from nexus.services.skills.skill_service import SkillService as _SkillService

            skill_service = _SkillService(gateway=gateway)
            logger.debug("[BOOT:WIRED] SkillService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SkillService unavailable: %s", exc)
    elif not _on("skills"):
        logger.debug("[BOOT:WIRED] SkillService disabled by profile")

    # --- SkillPackageService: Skill export/import/validate (Issue #2035) ---
    skill_package_service: Any = getattr(brick_services, "skill_package_service", None)
    if skill_package_service is None and _on("skills") and skill_service is not None:
        try:
            from nexus.skills.package_service import SkillPackageService as _SkillPkgSvc

            skill_package_service = _SkillPkgSvc(
                fs=skill_service._fs,
                perms=skill_service._perms,
                skill_service=skill_service,
            )
            logger.debug("[BOOT:WIRED] SkillPackageService created")
        except Exception:
            pass  # Optional, may not be importable

    # --- SearchService: Search operations ---
    search_service: Any = None
    if _on("search"):
        try:
            from nexus.services.search.search_service import SearchService

            search_service = SearchService(
                metadata_store=nx.metadata,
                permission_enforcer=getattr(nx, "_permission_enforcer", None),
                router=kernel_services.router,
                rebac_manager=system_services.rebac_manager,
                enforce_permissions=getattr(nx, "_enforce_permissions", True),
                default_context=getattr(nx, "_default_context", None),
                record_store=getattr(nx, "_record_store", None),
                gateway=gateway,
            )
            logger.debug("[BOOT:WIRED] SearchService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] SearchService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] SearchService disabled by profile")

    # --- ShareLinkService: Share link operations ---
    share_link_service: Any = None
    if _on("discovery"):
        try:
            from nexus.services.share_link.share_link_service import ShareLinkService

            share_link_service = ShareLinkService(
                gateway=gateway,
                enforce_permissions=getattr(nx, "_enforce_permissions", True),
            )
            logger.debug("[BOOT:WIRED] ShareLinkService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] ShareLinkService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] ShareLinkService disabled by profile")

    # --- EventsService: File watching + advisory locking ---
    events_service: Any = None
    if _on("ipc"):
        try:
            from nexus.system_services.lifecycle.events_service import EventsService

            metadata_cache = None
            if hasattr(nx.metadata, "_cache"):
                metadata_cache = nx.metadata._cache

            events_service = EventsService(
                backend=nx.backend,
                event_bus=brick_services.event_bus,
                lock_manager=brick_services.lock_manager,
                zone_id=None,
                metadata_cache=metadata_cache,
            )
            logger.debug("[BOOT:WIRED] EventsService created")
        except Exception as exc:
            logger.debug("[BOOT:WIRED] EventsService unavailable: %s", exc)
    else:
        logger.debug("[BOOT:WIRED] EventsService disabled by profile")

    # --- MetadataExportService: JSONL metadata export/import ---
    metadata_export_service: Any = None
    try:
        from nexus.factory._metadata_export import create_metadata_export_service

        metadata_export_service = create_metadata_export_service(nx)
    except Exception as exc:
        logger.debug("[BOOT:WIRED] MetadataExportService unavailable: %s", exc)

    result = {
        "rebac_service": rebac_service,
        "mount_service": mount_service,
        "gateway": gateway,
        "mount_core_service": mount_core_service,
        "sync_service": sync_service,
        "sync_job_service": sync_job_service,
        "mount_persist_service": mount_persist_service,
        "mcp_service": mcp_service,
        "llm_service": llm_service,
        "llm_subsystem": llm_subsystem,
        "oauth_service": oauth_service,
        "skill_service": skill_service,
        "skill_package_service": skill_package_service,
        "search_service": search_service,
        "share_link_service": share_link_service,
        "events_service": events_service,
        "task_queue_service": brick_services.task_queue_service,
        "metadata_export_service": metadata_export_service,
    }

    elapsed = time.perf_counter() - t0
    active = sum(1 for v in result.values() if v is not None)
    logger.info("[BOOT:WIRED] %d/%d services ready (%.3fs)", active, len(result), elapsed)
    return result
