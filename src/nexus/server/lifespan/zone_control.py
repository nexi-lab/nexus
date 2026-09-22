"""zone-v1 service assembly and readiness gating (2C).

Arms the ZoneApplicationService / AuthorizationService / operation worker on
app.state and — the fail-closed rule — refuses to report ready when a
mandatory provider (runtime port, zone store, epoch store) is missing. A
degraded boot that silently answers allow-all or phantom-active zones is the
failure mode this exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from nexus.remote.zone_runtime_client import NullZoneRuntimePort, ZoneRuntimePort
from nexus.services.zones.authz import AuthorizationService
from nexus.services.zones.service import ZoneApplicationService

logger = logging.getLogger(__name__)


class ZoneControlNotArmed(RuntimeError):
    """Raised (or reported) when a mandatory zone provider is missing."""


def arm_zone_services(
    app: FastAPI,
    *,
    session_factory: Any,
    runtime: ZoneRuntimePort | None = None,
    rebac_check: Any = None,
    projection_write: Any = None,
    projection_delete: Any = None,
    membership_check: Any = None,
    trusted_issuers: frozenset[str] = frozenset(),
    auth_armed: bool = True,
    transfer_policy: Any = None,
    transfer_executor: Any = None,
    worker_enabled: bool = True,
    inline_execution: bool | None = None,
) -> dict[str, Any]:
    """Assemble the zone services onto app.state; returns a readiness report.

    ``runtime=None`` arms the Null port: mutations become explicitly
    unavailable (fail-closed) rather than phantom-succeeding. Readiness
    reports armed=False in that case so full-profile startup checks and
    orchestrators can refuse.
    """
    runtime = runtime or NullZoneRuntimePort()
    rebac_check = rebac_check or _deny_all_rebac
    if session_factory is None:
        raise ZoneControlNotArmed("zone canonical store is unavailable")

    service = ZoneApplicationService(
        session_factory,
        runtime,
        worker_enabled=worker_enabled if inline_execution is None else inline_execution,
        projection_write=projection_write,
        projection_delete=projection_delete,
        transfer_policy=transfer_policy,
        transfer_executor=transfer_executor,
    )
    authz = AuthorizationService(
        session_factory,
        rebac_check,
        membership_check=membership_check,
        trusted_issuers=trusted_issuers,
    )

    app.state.zone_application_service = service
    app.state.zone_authorization_service = authz
    app.state.zone_session_factory = session_factory
    app.state.zone_runtime = runtime

    # P1a SessionRuntimeService (§8.9): home-zone record routing goes through
    # the typed kernel — real VFS bytes with a zone-scoped OperationContext,
    # never SQL columns standing in for zone I/O.
    fs = getattr(app.state, "nexus_fs", None)

    def _zone_fs_writer(path: str, buf: bytes, zone_id: str) -> int:
        if fs is None:  # pragma: no cover - guarded by composite arming
            raise RuntimeError("zone filesystem unavailable")
        from nexus.contracts.types import OperationContext
        from nexus.lib.zone_scoping import scope_single_path

        ctx = OperationContext(
            user_id="session-runtime",
            subject_type="service",
            subject_id="session-runtime",
            zone_id=zone_id,
            zone_perms=((zone_id, "rw"),),
            is_admin=False,
            groups=[],
        )
        scoped_path = scope_single_path(path, f"/zone/{zone_id}", zone_id)
        fs.write(path=scoped_path, buf=buf, context=ctx)
        return len(buf)

    from nexus.services.zones.session_runtime import SessionRuntimeService
    from nexus.services.zones.session_tasks import SessionTaskService

    app.state.session_runtime_service = SessionRuntimeService(
        session_factory, fs_writer=_zone_fs_writer if fs is not None else None
    )
    app.state.session_task_service = SessionTaskService(
        session_factory, fs_writer=_zone_fs_writer if fs is not None else None
    )

    report = {
        "zone_store": session_factory is not None,
        "auth_armed": auth_armed,
        "zone_runtime_armed": _runtime_ready(runtime),
        "rebac_armed": rebac_check is not _deny_all_rebac,
        "grant_projection_armed": projection_write is not None and projection_delete is not None,
        "delegation_membership_armed": membership_check is not None,
        "transfer_armed": transfer_policy is not None and transfer_executor is not None,
        "worker_enabled": worker_enabled,
    }
    report["composite_armed"] = all(
        [
            report["zone_store"],
            report["auth_armed"],
            report["zone_runtime_armed"],
            report["rebac_armed"],
            report["grant_projection_armed"],
        ]
    )
    app.state.zone_control_readiness = report
    if not report["composite_armed"]:
        logger.warning("zone control NOT fully armed: %s", report)
    return report


def zone_worker(app: FastAPI) -> Any:
    """The pump loop driver for background workers (lease/fence-safe)."""
    from nexus.services.zones.worker import ZoneOperationWorker

    service = app.state.zone_application_service
    authz = app.state.zone_authorization_service

    def runtime_dependency_is_current(
        delegation_id: str, zone_id: str, grant_ref: str, authorization_epoch: int
    ) -> bool:
        from nexus.services.zones.authz import Principal
        from nexus.storage.models import ZoneDelegationModel

        try:
            with app.state.zone_session_factory() as session:
                delegation = session.get(ZoneDelegationModel, delegation_id)
                if (
                    delegation is None
                    or delegation.zone_id != zone_id
                    or delegation.grant_id != grant_ref
                    or int(delegation.epoch) != authorization_epoch
                ):
                    return False
                current = authz.verify_delegation(
                    session, delegation_id=delegation_id, audience="nexus-api"
                )
                if not current:
                    return False
                allowed = authz.allow(
                    session,
                    principal=Principal(subject_type="organization", subject_id=delegation.org_id),
                    zone_id=zone_id,
                    capability="zone.runtime.execute",
                    resource_path="/",
                )
                return bool(allowed)
        except Exception:
            return False

    return ZoneOperationWorker(
        app.state.zone_session_factory,
        app.state.zone_runtime,
        service,
        session_runtime=app.state.session_runtime_service,
        session_tasks=app.state.session_task_service,
        runtime_dependency_validator=runtime_dependency_is_current,
    )


def _deny_all_rebac(session: Any, subject: str, permission: str, obj: str, zone_id: str) -> bool:  # noqa: ARG001
    """Placeholder ReBAC binding: denies everything.

    Grant∩ReBAC with this binding can never allow — which is the correct
    behavior for an assembly that failed to wire the real ReBAC store
    (§5.4: store unavailable → deny).
    """
    logger.debug("rebac not armed — denying %s %s %s", subject, permission, obj)
    return False


def _runtime_ready(runtime: ZoneRuntimePort) -> bool:
    if isinstance(runtime, NullZoneRuntimePort):
        return False
    try:
        capabilities = set(runtime.probe_capabilities(ctx={"operation_id": "readiness"}))
    except Exception:
        logger.exception("zone runtime capability probe failed")
        return False
    required = {
        "zone-runtime:create",
        "zone-runtime:join",
        "zone-runtime:status",
        "zone-runtime:mount",
        "zone-runtime:unmount",
        "zone-runtime:deprovision",
        "zone-runtime:operation-journal",
    }
    # The pinned 763f8c0 runtime snapshots ``permission.provider_armed``
    # before service declarations install the ReBAC provider.  The Rust full
    # profile independently refuses startup if that provider is absent, while
    # this assembly separately requires the live Python ReBAC/projection
    # bindings in ``composite_armed``.  Do not duplicate that gate using the
    # stale bootstrap snapshot; this check owns only runtime/auth readiness.
    return required <= capabilities


def _rebac_bindings(
    manager: Any,
) -> tuple[Callable[..., bool], Callable[..., None], Callable[..., None]]:
    def check(_session: Any, subject: str, permission: str, path: str, zone_id: str) -> bool:
        subject_type, subject_id = subject.split(":", 1)
        target = "/*" if path == "/" else path
        return bool(
            manager.rebac_check(
                (subject_type, subject_id),
                permission,
                ("file", target),
                zone_id=zone_id,
                consistency="strong",
            )
        )

    def write(zone_id: str, principal: dict[str, Any], relation: str, path: str) -> None:
        target = "/*" if path == "/" else f"{path.rstrip('/')}/*"
        manager.rebac_write(
            (str(principal["subject_type"]), str(principal["subject_id"])),
            relation,
            ("file", target),
            zone_id=zone_id,
        )

    def delete(zone_id: str, principal: dict[str, Any], relation: str, path: str) -> None:
        target = "/*" if path == "/" else f"{path.rstrip('/')}/*"
        rows = manager.rebac_list_tuples(
            subject=(str(principal["subject_type"]), str(principal["subject_id"])),
            relation=relation,
            object=("file", target),
            subject_relation=None,
            zone_id=zone_id,
        )
        for row in rows:
            manager.rebac_delete(str(row["tuple_id"]))

    return check, write, delete


async def startup_zone_control(app: FastAPI) -> list[asyncio.Task[Any]]:
    """Wire zone-v1 once the record store and ReBAC brick are available."""
    configured = os.environ.get("NEXUS_ZONE_CONTROL_ENABLED")
    explicitly_enabled = configured is not None and configured.lower() in {"1", "true", "yes"}
    explicitly_disabled = configured is not None and not explicitly_enabled
    session_factory = getattr(app.state, "session_factory", None)
    rebac_manager = getattr(app.state, "rebac_manager", None)
    nexus_fs = getattr(app.state, "nexus_fs", None)
    auto_enabled = (
        session_factory is not None and rebac_manager is not None and nexus_fs is not None
    )
    enabled = explicitly_enabled or (not explicitly_disabled and auto_enabled)
    if not enabled:
        app.state.zone_control_readiness = {"enabled": False, "composite_armed": False}
        return []

    runtime: ZoneRuntimePort | None = None
    if nexus_fs is not None and hasattr(nexus_fs, "zone_runtime_call"):
        from nexus.remote.zone_runtime_client import KernelRpcZoneRuntimePort

        runtime = KernelRpcZoneRuntimePort(nexus_fs)

    rebac_check = projection_write = projection_delete = None
    if rebac_manager is not None:
        rebac_check, projection_write, projection_delete = _rebac_bindings(rebac_manager)

    issuers = frozenset(
        value.strip()
        for value in os.environ.get("NEXUS_ZONE_DELEGATION_ISSUERS", "").split(",")
        if value.strip()
    )
    report = arm_zone_services(
        app,
        session_factory=session_factory,
        runtime=runtime,
        rebac_check=rebac_check,
        projection_write=projection_write,
        projection_delete=projection_delete,
        membership_check=getattr(app.state, "moss_membership_verifier", None),
        trusted_issuers=issuers,
        worker_enabled=True,
        # The background worker owns the operation lease in deployed apps.
        # Executing the same mutation inline would race that worker.
        inline_execution=False,
        auth_armed=bool(
            getattr(app.state, "api_key", None) or getattr(app.state, "auth_provider", None)
        ),
        transfer_policy=getattr(app.state, "zone_transfer_policy", None),
        transfer_executor=getattr(app.state, "zone_transfer_executor", None),
    )
    report["enabled"] = True
    required = explicitly_enabled or getattr(app.state, "deployment_profile", None) == "full"
    if required and not report["composite_armed"]:
        raise ZoneControlNotArmed(f"mandatory zone providers missing: {report}")

    worker = zone_worker(app)
    stop = asyncio.Event()
    app.state.zone_worker_stop = stop

    async def run() -> None:
        while not stop.is_set():
            await asyncio.to_thread(worker.pump_once)
            await asyncio.to_thread(worker.reconcile_stale_operations)
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.25)
            except TimeoutError:
                continue

    return [asyncio.create_task(run(), name="zone-operation-worker")]


async def shutdown_zone_control(app: FastAPI) -> None:
    stop = getattr(app.state, "zone_worker_stop", None)
    if stop is not None:
        stop.set()
