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
from sqlalchemy.exc import SQLAlchemyError

from nexus.remote.zone_runtime_client import NullZoneRuntimePort, ZoneRuntimePort
from nexus.services.zones.authz import AuthorizationService
from nexus.services.zones.membership import MembershipUnreachable, MossMembershipVerifier
from nexus.services.zones.service import ZoneApplicationService

logger = logging.getLogger(__name__)

#: Worker loop cadence (seconds). Small for responsive outbox pickup.
WORKER_TICK_S = 0.25

#: Late-kernel-readiness retry cadence (engineering default): how often the
#: background re-arm loop re-probes a runtime that was not ready at startup.
REARM_TICK_S = 10.0
#: Grace window for the worker loop to finish an in-flight pump at shutdown
#: before the lifespan's blanket task-cancel pass takes over.
WORKER_GRACE_S = 5.0


class ZoneControlNotArmed(RuntimeError):
    """Raised (or reported) when a mandatory zone provider is missing."""


def arm_zone_services(
    app: FastAPI,
    *,
    session_factory: Any,
    runtime: ZoneRuntimePort | None = None,
    rebac_check: Any = None,
    rebac_invalidate: Any = None,
    projection_write: Any = None,
    projection_delete: Any = None,
    membership_check: Any = None,
    worker_membership_check: Any = None,
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

    # P1a SessionRuntimeService (§8.9): home-zone record routing goes through
    # the typed kernel — real VFS bytes with a zone-scoped OperationContext,
    # never SQL columns standing in for zone I/O.
    fs = getattr(app.state, "nexus_fs", None)

    def _zone_fs_purger(zone_id: str) -> None:
        """§5.6 deprovision physical replica teardown: clear the root-zone
        VFS materialized tree /zone/<zone_id>/ written by _zone_fs_writer.

        Pass criteria is "no file entries remain under the prefix" — the
        delete_batch success flag is unreliable here (its recheck reports
        "Path recreated" on an emptied implicit-dir tree), so verify the
        listing directly. Idempotent: an already-purged tree passes.
        """
        from nexus.contracts.types import OperationContext

        if fs is None:  # pragma: no cover - guarded by composite arming
            raise RuntimeError("zone filesystem unavailable")
        ctx = OperationContext(
            user_id="zone-deprovision",
            subject_type="service",
            subject_id="zone-deprovision",
            zone_id=zone_id,
            zone_perms=((zone_id, "rw"),),
            is_admin=False,
            groups=[],
        )
        prefix = f"/zone/{zone_id}"
        fs.delete_batch(paths=[prefix], recursive=True, context=ctx)
        # Empty implicit-dir skeletons (no inode) survive the batch delete —
        # they carry no data. Pass = no FILE entries remain: stat each listed
        # entry (implicit dirs stat as entry_type DT_DIR) and fail on any
        # non-directory.
        try:
            entries = fs.sys_readdir(prefix, recursive=True)
        except Exception:  # noqa: BLE001 - a vanished prefix is the goal
            entries = []
        remaining = []
        for entry in entries:
            path = entry if isinstance(entry, str) else getattr(entry, "path", str(entry))
            stat = fs.sys_stat(path)
            if stat is not None and stat.get("entry_type") != 1:  # 1 = DT_DIR
                remaining.append(path)
        if remaining:  # pragma: no cover - defensive: purge must drain files
            raise RuntimeError(
                f"zone {zone_id}: {len(remaining)} file entries remain after VFS purge"
            )

    service = ZoneApplicationService(
        session_factory,
        runtime,
        worker_enabled=worker_enabled if inline_execution is None else inline_execution,
        projection_write=projection_write,
        projection_delete=projection_delete,
        transfer_policy=transfer_policy,
        transfer_executor=transfer_executor,
        zone_fs_purger=_zone_fs_purger if fs is not None else None,
        rebac_invalidate=rebac_invalidate,
    )
    authz = AuthorizationService(
        session_factory,
        rebac_check,
        membership_check=membership_check,
        trusted_issuers=trusted_issuers,
    )
    worker_authz = AuthorizationService(
        session_factory,
        rebac_check,
        membership_check=(
            worker_membership_check if worker_membership_check is not None else membership_check
        ),
        trusted_issuers=trusted_issuers,
    )

    app.state.zone_application_service = service
    app.state.zone_authorization_service = authz
    app.state.zone_worker_authorization_service = worker_authz
    app.state.zone_session_factory = session_factory
    app.state.zone_runtime = runtime

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
    authz = app.state.zone_worker_authorization_service

    def runtime_dependency_is_current(
        delegation_id: str,
        zone_id: str,
        grant_ref: str,
        authorization_epoch: int,
        session_id: str,
    ) -> bool | None:
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
                    session,
                    delegation_id=delegation_id,
                    audience="nexus-api",
                    capability="zone.runtime.execute",
                    resource_path=f"/sessions/{session_id}",
                )
                if not current:
                    if current.code == "MEMBERSHIP_UNAVAILABLE":
                        return None
                    return False
                allowed = authz.allow(
                    session,
                    principal=Principal(subject_type="organization", subject_id=delegation.org_id),
                    zone_id=zone_id,
                    capability="zone.runtime.execute",
                    resource_path="/",
                )
                return bool(allowed)
        except MembershipUnreachable:
            return None
        except SQLAlchemyError:
            # Store trouble is unknown, not "dependency gone": a transient DB
            # error must not park runs (that would be fail-closed on the wrong
            # axis) — keep the dependency retrying instead.
            logger.exception("dependency revalidation hit a store error")
            return None
        except Exception:
            # A validator bug is NOT a dependency verdict: batch-parking
            # active runs on a code defect is fail-closed on the wrong axis.
            # Unknown (None) keeps the dependency retrying; the stack stays
            # in the log for the fix.
            logger.exception("dependency revalidation raised unexpectedly")
            return None

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
    # The pinned bc89aa6 runtime snapshots ``permission.provider_armed``
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
        parts = [part for part in path.split("/") if part]
        targets = [path]
        targets.extend(f"/{'/'.join(parts[:depth])}/*" for depth in range(len(parts) - 1, 0, -1))
        targets.append("/*")
        return any(
            bool(
                manager.rebac_check(
                    (subject_type, subject_id),
                    permission,
                    ("file", target),
                    zone_id=zone_id,
                    consistency="strong",
                )
            )
            for target in dict.fromkeys(targets)
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
    rebac_invalidate = None
    if rebac_manager is not None:
        rebac_check, projection_write, projection_delete = _rebac_bindings(rebac_manager)
        # The deprovision purge bypasses the ReBAC writer (raw SQL deletes),
        # so cached permission decisions outlive the zone unless flushed.
        if hasattr(rebac_manager, "clear_permission_cache"):
            rebac_invalidate = rebac_manager.clear_permission_cache

    issuers = frozenset(
        value.strip()
        for value in os.environ.get("NEXUS_ZONE_DELEGATION_ISSUERS", "").split(",")
        if value.strip()
    )
    access_membership = MossMembershipVerifier.from_env(cache_ttl_s=0)
    worker_membership = MossMembershipVerifier.from_env(cache_ttl_s=5)
    if issuers and (access_membership is None or worker_membership is None):
        raise ZoneControlNotArmed(
            "NEXUS_ZONE_DELEGATION_ISSUERS requires both "
            "NEXUS_ZONE_MEMBERSHIP_URL and NEXUS_ZONE_MEMBERSHIP_TOKEN"
        )
    app.state.moss_membership_verifier = (
        access_membership.check if access_membership is not None else None
    )
    # kept for shutdown: the verifiers own httpx clients that must close
    app.state.zone_membership_verifiers = (access_membership, worker_membership)

    def worker_membership_check(user_id: str, org_id: str, version: str) -> bool:
        if worker_membership is None:
            return False
        state = worker_membership.check_detailed(user_id, org_id, version)
        if state == "unreachable":
            raise MembershipUnreachable("Moss membership lookup unavailable")
        return state == "ok"

    async def arm_once() -> dict[str, Any]:
        # The capability probe is a synchronous gRPC call (up to 30s): run
        # the whole assembly off the event loop so startup stays responsive.
        report = await asyncio.to_thread(
            arm_zone_services,
            app,
            session_factory=session_factory,
            runtime=runtime,
            rebac_check=rebac_check,
            rebac_invalidate=rebac_invalidate,
            projection_write=projection_write,
            projection_delete=projection_delete,
            membership_check=app.state.moss_membership_verifier,
            worker_membership_check=(worker_membership_check if worker_membership else None),
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
        return report

    report = await arm_once()
    # Fail-closed on missing providers ONLY for deployments that explicitly
    # declare zone control (NEXUS_ZONE_CONTROL_ENABLED). A bare
    # deployment_profile == "full" must not make the whole app unbootable:
    # "full" is also the default profile of deployments that never opted
    # into the zone surface (the self-contained watch/e2e stack), and those
    # must keep booting with the zone endpoints answering 503 instead.
    required = explicitly_enabled
    if required and not report["composite_armed"]:
        raise ZoneControlNotArmed(f"mandatory zone providers missing: {report}")

    def start_worker() -> asyncio.Task[Any]:
        worker = zone_worker(app)
        stop = asyncio.Event()
        app.state.zone_worker_stop = stop

        async def run() -> None:
            while not stop.is_set():
                try:
                    await asyncio.to_thread(worker.pump_once)
                    await asyncio.to_thread(worker.reconcile_stale_operations)
                except Exception:
                    logger.exception("zone operation worker iteration failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=WORKER_TICK_S)
                except TimeoutError:
                    continue

        return asyncio.create_task(run(), name="zone-operation-worker")

    # The recovery worker starts UNCONDITIONALLY: the outbox retry loop is
    # itself the crash-recovery mechanism and tolerates an unreachable
    # runtime (a probe failure is often just a kernel still warming —
    # gating the worker on the probe made a slow boot burn the whole outbox
    # retry budget before the first pump, fault class 1-2-3). The readiness
    # probe only governs the HTTP surface's 503 answers, not the recovery
    # loop.
    tasks: list[asyncio.Task[Any]] = [start_worker()]
    if not report["composite_armed"]:
        # The kernel may legitimately still be warming when the Python
        # lifespan starts (its zone-runtime service registers late): retry
        # the probe in the background instead of pinning the zone surface
        # at 503 for the whole process lifetime. Only the runtime leg can
        # heal this way — the store/rebac/auth legs are decided here.
        async def rearm_until_ready() -> None:
            while True:
                await asyncio.sleep(REARM_TICK_S)
                try:
                    retry = await arm_once()
                except Exception:
                    logger.exception("zone control re-arm attempt failed")
                    continue
                if retry["composite_armed"]:
                    logger.info("zone control armed after late runtime readiness")
                    return

        tasks.append(asyncio.create_task(rearm_until_ready(), name="zone-control-rearm"))
    app.state.zone_worker_tasks = tasks
    return tasks


async def shutdown_zone_control(app: FastAPI) -> None:
    """Graceful worker stop: signal, then bounded-join the loop so an
    in-flight pump finishes its transaction; the lifespan's blanket cancel
    pass remains the backstop for whatever outlives the grace window."""
    stop = getattr(app.state, "zone_worker_stop", None)
    if stop is not None:
        stop.set()
    tasks = getattr(app.state, "zone_worker_tasks", None)
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=WORKER_GRACE_S
            )
        except TimeoutError:
            logger.warning(
                "zone worker did not stop within %.0fs; leaving it to the cancel pass",
                WORKER_GRACE_S,
            )
    for verifier in getattr(app.state, "zone_membership_verifiers", ()) or ():
        if verifier is not None:
            verifier.close()
