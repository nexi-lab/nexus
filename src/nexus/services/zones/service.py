"""ZoneApplicationService — the only writer of zone product state (2C, §5).

Sagas implemented exactly as §5 freezes them:

- create: validate + capability → transaction (reserve id + operation +
  outbox + audit.accepted) → worker claims (lease/generation/fence) →
  runtime create via the typed port → read back physical identity →
  mandatory initial grant → mark active with the receipt (audit.completed).
  Any failure keeps the zone non-active, records step/error/retryability,
  never swaps the zone id, and never assumes SQL rollback undid the runtime.
- grant issuance: pending + projection outbox + epoch intent + audit in one
  transaction → projections applied → epoch advanced atomically → active.
  A failed mandatory projection leaves the grant pending and access denied.
- revoke: revoked fact + epoch advance + invalidation outbox in ONE
  transaction, so a crash after commit still fails closed at every boundary.
- deprovision: deleting → refuse new grants/runtimes/mounts → blockers
  checked → grants revoked → deletion epoch/tombstone → per-replica
  receipts → deleted only after required acknowledgements.

Idempotency (§5.7): mutations carry a key scoped to principal+action+target
plus a canonical request hash; same key + same hash replays the recorded
operation, same key + different hash is IDEMPOTENCY_CONFLICT. A network
timeout means unknown — poll the operation — never re-key and rebuild.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nexus.bricks.auth.constants import RESERVED_ZONE_IDS
from nexus.contracts.zone_v1 import (
    KNOWN_CAPABILITIES,
    ZoneCreateRequest,
    ZoneGrantCreateRequest,
    ZonePatchRequest,
)
from nexus.remote.zone_runtime_client import (
    NullZoneRuntimePort,
    RuntimeReceipt,
    ZoneRuntimePort,
    ZoneRuntimeRejected,
    ZoneRuntimeUnavailable,
)
from nexus.storage.models import (
    RebacRelationSourceModel,
    SessionDataRecordModel,
    SessionModel,
    SessionRuntimeRunModel,
    SessionZoneDependencyModel,
    TaskAttemptModel,
    TaskResolutionModel,
    TaskSpecModel,
    ZoneAuthorizationEpochModel,
    ZoneDelegationModel,
    ZoneGrantModel,
    ZoneGrantProjectionOutboxModel,
    ZoneMountModel,
    ZoneOperationModel,
    ZoneRuntimeOutboxModel,
)
from nexus.storage.models.auth import ZoneModel

logger = logging.getLogger(__name__)

GENESIS_CAPABILITIES = tuple(sorted(KNOWN_CAPABILITIES))

_CAPABILITY_RELATIONS = {
    "zone.data.read": "direct_viewer",
    "zone.data.export": "direct_viewer",
    "zone.data.write": "direct_editor",
    "zone.runtime.execute": "direct_owner",
    "zone.metadata.manage": "direct_owner",
    "zone.grants.manage": "direct_owner",
    "zone.lifecycle.delete": "direct_owner",
}


class ServiceError(Exception):
    """Stable-code error surfaced as ErrorInfo; clients judge by code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        http_status: int = 400,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.http_status = http_status
        # Structured payload for actionable refusals (e.g. ZONE_DELETE_BLOCKED
        # blocker identities); None keeps the wire shape unchanged.
        self.details = details


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _request_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _subject_key(principal: dict[str, Any]) -> str:
    return f"{principal.get('subject_type', 'user')}:{principal.get('subject_id', '')}"


def _scope_subject(principal: dict[str, Any]) -> str:
    """Idempotency-scope subject: type-qualified so a user and an agent or
    service sharing a subject_id never collide in one (scope, key) domain.
    Mirrors _subject_key's encoding for the is_owner_or_admin comparison."""
    return f"{principal.get('subject_type') or 'user'}:{principal.get('subject_id')}"


def _cleanup_zone_projections(session: Session, zone_id: str) -> None:
    """Remove zone-owned graph/ReBAC projections after runtime deletion.

    Canonical Zone/Grant/operation rows are retained as history.  These
    materialized graph rows are deleted only after the typed runtime returned
    a successful deprovision receipt, never inline at the HTTP boundary.
    """
    tables = set(inspect(session.get_bind()).get_table_names())
    if {"entity_mentions", "entities"} <= tables:
        session.execute(
            text(
                "DELETE FROM entity_mentions WHERE entity_id IN ("
                "SELECT entity_id FROM entities WHERE zone_id = :zone_id)"
            ),
            {"zone_id": zone_id},
        )
    if {"relationships", "entities"} <= tables:
        session.execute(
            text(
                "DELETE FROM relationships WHERE zone_id = :zone_id "
                "OR source_entity_id IN (SELECT entity_id FROM entities WHERE zone_id = :zone_id) "
                "OR target_entity_id IN (SELECT entity_id FROM entities WHERE zone_id = :zone_id)"
            ),
            {"zone_id": zone_id},
        )
    if "entities" in tables:
        session.execute(text("DELETE FROM entities WHERE zone_id = :zone_id"), {"zone_id": zone_id})
    if "rebac_tuples" in tables:
        session.execute(
            text(
                "DELETE FROM rebac_tuples WHERE zone_id = :zone_id "
                "OR subject_zone_id = :zone_id OR object_zone_id = :zone_id"
            ),
            {"zone_id": zone_id},
        )
        if "rebac_version_sequences" in tables:
            # Same-store revision bump: raw-SQL deletions bypass the tuple
            # writer, so without this other replicas keep serving cached
            # decisions for the deleted tuples until their TTL expires.
            session.execute(
                text(
                    "UPDATE rebac_version_sequences "
                    "SET current_version = current_version + 1, updated_at = :now "
                    "WHERE zone_id = :zone_id"
                ),
                {"zone_id": zone_id, "now": _now()},
            )


def _purge_zone_session_data(session: Session, zone_id: str) -> None:
    """Remove the zone's session/task business rows after runtime deletion.

    §5.6 "remove physical replicas": the SQLite rows backing the /v2
    session/task read surface are the direct data source of the read-back
    defect — unlike the Zone/Grant/operation rows retained as history, these
    business rows must not outlive the zone. Deletion order follows the FK
    graph (all RESTRICT/CASCADE edges verified against the models): the two
    RESTRICT edges invisible to nothing but easy to misorder are
    session_runtime_runs.attempt_id → task_attempts and
    task_attempts.resolution_id → task_resolutions — on PostgreSQL (FKs
    enforced; SQLite connections run without PRAGMA foreign_keys) deleting
    attempts before runs makes deprovision permanently fail.

    Cross-zone boundary: attempts/resolutions belong to the task's home
    zone (their VFS bytes materialize under the home zone's tree), so they
    purge by ``zone_id`` only; runs record execution placement and purge by
    execution zone OR home zone — mirroring the blocker query below.
    """
    home_session_ids = select(SessionModel.session_id).where(SessionModel.home_zone_id == zone_id)
    run_delete_set = (
        select(SessionRuntimeRunModel.pid)
        .where(
            (SessionRuntimeRunModel.execution_zone_id == zone_id)
            | (SessionRuntimeRunModel.session_id.in_(home_session_ids))
        )
        .scalar_subquery()
    )
    session.execute(
        delete(SessionZoneDependencyModel).where(
            (SessionZoneDependencyModel.zone_id == zone_id)
            | (SessionZoneDependencyModel.pid.in_(run_delete_set))
        )
    )
    session.execute(
        delete(SessionRuntimeRunModel).where(SessionRuntimeRunModel.pid.in_(run_delete_set))
    )
    session.execute(delete(TaskAttemptModel).where(TaskAttemptModel.zone_id == zone_id))
    session.execute(delete(TaskResolutionModel).where(TaskResolutionModel.zone_id == zone_id))
    session.execute(delete(TaskSpecModel).where(TaskSpecModel.zone_id == zone_id))
    session.execute(delete(SessionDataRecordModel).where(SessionDataRecordModel.zone_id == zone_id))
    session.execute(delete(SessionModel).where(SessionModel.home_zone_id == zone_id))
    # authorization-domain rows that must not outlive the zone either: the
    # projection provenance ledger and the delegation projections (a stale
    # active ledger row would also poison later cleanup's remaining-check)
    session.execute(
        delete(RebacRelationSourceModel).where(RebacRelationSourceModel.zone_id == zone_id)
    )
    session.execute(delete(ZoneDelegationModel).where(ZoneDelegationModel.zone_id == zone_id))


def _projection_edges(grant: ZoneGrantModel) -> list[tuple[dict[str, Any], str, str]]:
    """Translate one grant into deterministic, provenance-addressable ReBAC edges."""
    prefixes = grant.resource_prefixes or ["/"]
    relations = {_CAPABILITY_RELATIONS[c] for c in grant.capabilities if c in _CAPABILITY_RELATIONS}
    return [
        (grant.grantee, relation, resource_path)
        for resource_path in prefixes
        for relation in sorted(relations)
    ]


@dataclass
class OperationResult:
    operation_id: str
    state: str
    step: str
    retryable: bool
    zone_status: str | None = None


class ZoneApplicationService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        runtime: ZoneRuntimePort | None = None,
        *,
        worker_enabled: bool = False,
        projection_write: Callable[[str, dict[str, Any], str, str], None] | None = None,
        projection_delete: Callable[[str, dict[str, Any], str, str], None] | None = None,
        transfer_policy: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], bool]
        | None = None,
        transfer_executor: Callable[[dict[str, Any], dict[str, Any], str], dict[str, Any]]
        | None = None,
        zone_fs_purger: Callable[[str], None] | None = None,
        rebac_invalidate: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._runtime: ZoneRuntimePort = runtime or NullZoneRuntimePort()
        # 2C arms the worker; until assembly says so, mutations stay
        # capability-unavailable (B3 item 10 carried into 2C wiring).
        self._worker_enabled = worker_enabled
        self._projection_write = projection_write
        self._projection_delete = projection_delete
        self._transfer_policy = transfer_policy
        self._transfer_executor = transfer_executor
        # §5.6 physical replica teardown: clears the root-zone VFS
        # materialized tree (/zone/<zone_id>/) after the runtime receipt.
        # None (unarmed assembly) skips the VFS leg; SQLite rows are
        # always purged by _purge_zone_session_data.
        self._zone_fs_purger = zone_fs_purger
        # The deprovision purge deletes rebac tuples with raw SQL, which
        # bypasses the ReBAC writer — no revision moves, so cached
        # permission decisions would keep answering allow for a deleted
        # zone. The assembly binds the manager's cache-clear here; None
        # (unarmed assembly) leaves the flush to the next natural
        # invalidation.
        self._rebac_invalidate = rebac_invalidate

    @staticmethod
    def _require_principal(principal: dict[str, Any]) -> None:
        if not principal.get("subject_id") or not principal.get("subject_type"):
            raise ServiceError(
                "RESOURCE_RELATION_DENIED",
                "authenticated principal is required",
                http_status=403,
            )

    @staticmethod
    def _audit(event: str, *, principal: dict[str, Any], zone_id: str | None, **meta: Any) -> None:
        """Emit through Nexus' durable activity pipeline when it is armed."""
        from nexus.contracts.protocols.activity import EventKind, Result, emit

        emit(
            kind=EventKind.OP,
            result=Result.OK,
            actor_user=str(principal.get("subject_id") or "unknown"),
            subject_zone=zone_id,
            meta={"event": event, **meta},
        )

    # ── idempotency bookkeeping ─────────────────────────────────────────────

    def _claim_idempotency(
        self, session: Session, *, scope: str, key: str, req_hash: str
    ) -> ZoneOperationModel | None:
        existing = session.execute(
            select(ZoneOperationModel).where(
                ZoneOperationModel.idempotency_scope == scope,
                ZoneOperationModel.idempotency_key == key,
            )
        ).scalar_one_or_none()
        if existing is None:
            return None
        if existing.request_hash != req_hash:
            raise ServiceError(
                "IDEMPOTENCY_CONFLICT", "key reused with a different request", http_status=409
            )
        return existing

    def _new_operation(
        self,
        session: Session,
        *,
        action: str,
        zone_id: str | None,
        scope: str,
        key: str,
        req_hash: str,
        grant_id: str | None = None,
    ) -> ZoneOperationModel:
        op = ZoneOperationModel(
            operation_id=_new_id("op"),
            action=action,
            zone_id=zone_id,
            grant_id=grant_id,
            state="queued",
            step="accepted",
            retryable=True,
            idempotency_scope=scope,
            idempotency_key=key,
            request_hash=req_hash,
            generation=0,
            fence=0,
        )
        session.add(op)
        try:
            session.flush()
        except IntegrityError as exc:
            # A concurrent request with the same (scope, key) committed
            # first — the claim check could not see it. Surface the race as
            # the idempotency conflict the client resolves by retrying (the
            # retry replays the stored operation), never an unhandled 500.
            raise ServiceError(
                "IDEMPOTENCY_CONFLICT",
                "a concurrent request holds this idempotency key",
                http_status=409,
            ) from exc
        return op

    # ── create saga (§5.2) ───────────────────────────────────────────────────

    def create_zone(
        self,
        request: ZoneCreateRequest,
        *,
        idempotency_key: str,
        principal: dict[str, Any],
    ) -> OperationResult:
        self._require_principal(principal)
        if request.zone_id in RESERVED_ZONE_IDS:
            raise ServiceError(
                "RESERVED_ZONE_ID",
                f"zone id {request.zone_id!r} is reserved",
                http_status=400,
            )
        scope = json.dumps([_scope_subject(principal), "zone.create", request.zone_id])
        req_hash = _request_hash(request.model_dump(mode="json"))
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(
                session, scope=scope, key=idempotency_key, req_hash=req_hash
            )
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )

            exists = session.get(ZoneModel, request.zone_id)
            if exists is not None and exists.canonical_status == "deleted":
                # A deprovisioned tombstone keeps the row for history, but the
                # id is reusable: rebuild in place instead of the 409 whose
                # hint says "deprovision it first" — following that hint used
                # to dead-end right back here.
                exists.name = request.display_name
                exists.display_name = request.display_name
                exists.description = request.description
                exists.phase = "Creating"
                exists.canonical_status = "creating"
                exists.canonical_revision = _new_id("rev")
                exists.created_by = principal
                exists.placement_location = (
                    request.deployment.location if request.deployment else "cloud"
                )
                exists.placement_data_domain = (
                    request.deployment.data_domain if request.deployment else None
                )
                exists.trust_domain = (
                    request.deployment.trust_domain
                    if request.deployment
                    else str(principal.get("trust_domain") or "local")
                )
                exists.placement_region = request.deployment.region if request.deployment else None
                exists.labels = request.labels
                exists.deleted_at = None
                zone = exists
            else:
                if exists is not None:
                    if exists.phase == "Creating":
                        # A create saga still owns the row: a client retrying
                        # with a fresh idempotency key gets the in-flight (or
                        # retryable-failed) operation replayed instead of a 409
                        # it cannot resolve.
                        pending = (
                            session.execute(
                                select(ZoneOperationModel).where(
                                    ZoneOperationModel.zone_id == request.zone_id,
                                    ZoneOperationModel.action == "create",
                                    or_(
                                        ZoneOperationModel.state.in_(("queued", "running")),
                                        (ZoneOperationModel.state == "failed")
                                        & ZoneOperationModel.retryable.is_(True),
                                    ),
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if pending is not None:
                            return OperationResult(
                                pending.operation_id,
                                pending.state,
                                pending.step,
                                pending.retryable,
                            )
                    raise ServiceError(
                        "ZONE_ALREADY_EXISTS",
                        f"zone {request.zone_id} already exists",
                        http_status=409,
                        details={
                            "hint": (
                                "a create that exhausted its outbox retries leaves the zone row "
                                "behind; deprovision it before recreating the id"
                            )
                        },
                    )

                zone = ZoneModel(
                zone_id=request.zone_id,
                name=request.display_name,  # legacy mirror column, mapper owns history
                display_name=request.display_name,
                description=request.description,
                phase="Creating",
                canonical_status="creating",  # not active — only a receipt makes it so
                canonical_revision=_new_id("rev"),
                created_by=principal,
                placement_location=request.deployment.location if request.deployment else "cloud",
                placement_data_domain=(
                    request.deployment.data_domain if request.deployment else None
                ),
                trust_domain=(
                    request.deployment.trust_domain
                    if request.deployment
                    else str(principal.get("trust_domain") or "local")
                ),
                placement_region=(request.deployment.region if request.deployment else None),
                labels=request.labels,
            )
            session.add(zone)
            if exists is None:
                # a tombstone rebuild reuses the surviving epoch row —
                # re-inserting it would trip the primary key
                session.add(
                    ZoneAuthorizationEpochModel(
                        zone_id=request.zone_id, epoch=0, reason="zone created"
                    )
                )
            op = self._new_operation(
                session,
                action="create",
                zone_id=request.zone_id,
                scope=scope,
                key=idempotency_key,
                req_hash=req_hash,
            )
            session.add(
                ZoneRuntimeOutboxModel(
                    operation_id=op.operation_id,
                    event_type="zone.create",
                    payload={
                        "zone_id": request.zone_id,
                        "request": request.model_dump(mode="json"),
                        "principal": principal,
                    },
                )
            )
            op.state = "queued"
            op.step = "accepted"
            result = OperationResult(op.operation_id, op.state, op.step, op.retryable)
            self._audit(
                "zone.create.accepted",
                principal=principal,
                zone_id=request.zone_id,
                operation_id=op.operation_id,
            )
        if self._worker_enabled:
            result = self._pump_create(operation_id=result.operation_id)
            if result.state == "waiting_dependency":
                with self._session_factory() as session:
                    stored_operation = session.get(ZoneOperationModel, result.operation_id)
                    grant_id = stored_operation.grant_id if stored_operation is not None else None
                if grant_id and self.complete_grant_projection(grant_id=grant_id):
                    current = self.get_operation(result.operation_id)
                    if current is not None:
                        return OperationResult(
                            result.operation_id,
                            str(current["state"]),
                            str(current["step"]),
                            bool(current["retryable"]),
                            zone_status="active" if current["state"] == "succeeded" else None,
                        )
            return result
        return result

    def _pump_create(self, *, operation_id: str) -> OperationResult:
        """Advance one create operation toward its receipt (worker step, §5.2.3-7).

        Runs inline for synchronous completions and from the worker loop;
        every write is fenced by lease owner + generation so a stale worker
        cannot overwrite a newer state.
        """
        with self._session_factory() as session, session.begin():
            op = session.get(ZoneOperationModel, operation_id)
            # Terminal means terminal; a failed-but-retryable create is NOT
            # terminal — the worker's stop criterion is exactly
            # ``failed and not retryable``, and outbox retries must be able
            # to re-drive it instead of spinning on a dead operation.
            if op is None or op.state == "succeeded" or (
                op.state == "failed" and not op.retryable
            ):
                return OperationResult(
                    op.operation_id if op else operation_id,
                    op.state if op else "unknown",
                    op.step if op else "gone",
                    False,
                )
            if op.step == "mandatory-grant-projection" and op.grant_id:
                return OperationResult(
                    op.operation_id,
                    "waiting_dependency",
                    op.step,
                    True,
                )
            zone_id = op.zone_id
            assert zone_id is not None
            zone_row = session.get(ZoneModel, zone_id)
            if zone_row is not None and zone_row.canonical_status in ("deleting", "deleted"):
                # The zone was deprovisioned while this create was in flight
                # (or waits for replay): re-creating the physical zone would
                # resurrect a deleted resource as an unreclaimable orphan.
                op.state = "failed"
                op.step = "zone-deleted"
                op.retryable = False
                op.error = {
                    "code": "ZONE_DELETED",
                    "message": f"zone {zone_id} was deprovisioned before the create replayed",
                    "retryable": False,
                }
                return OperationResult(operation_id, "failed", "zone-deleted", False)
            op.state = "running"
            op.step = "runtime-create"
            op.lease_owner = f"inline-{secrets.token_hex(4)}"
            op.generation += 1
            op.fence += 1
            fence = op.fence
        # Runtime effect outside the transaction: SQL rollback never pretends
        # the physical zone came back (§5.2 failure rules).
        receipt: RuntimeReceipt
        rejected_verdict: RuntimeReceipt | None = None
        try:
            try:
                create_receipt = self._runtime.create_zone(
                    zone_id=zone_id, ctx={"operation_id": operation_id}
                )
            except ZoneRuntimeUnavailable:
                # The mutation response may be lost or the runtime may report
                # its durable journal entry as PENDING.  Recover by polling the
                # same operation id; never mint a replacement id.
                recover = getattr(self._runtime, "get_operation", None)
                if recover is None:
                    raise
                create_receipt = recover(
                    operation_id=operation_id, ctx={"operation_id": operation_id}
                )
            except ZoneRuntimeRejected as exc:
                # The reconciliation poll answered with a deterministic
                # refusal — that IS a verdict on the operation. Surface it as
                # a rejected receipt so the failure branch below records a
                # terminal, non-retryable result instead of letting the
                # exception escape and be misfiled as unavailability.
                rejected_verdict = RuntimeReceipt(
                    ok=False, rejected=True, error=str(exc), raw={"rejected": str(exc)}
                )
            if rejected_verdict is not None:
                receipt = rejected_verdict
            elif not create_receipt.ok:
                receipt = create_receipt
            else:
                observed = self._runtime.zone_status(
                    zone_id=zone_id, ctx={"operation_id": operation_id}
                )
                receipt = RuntimeReceipt(
                    ok=observed.ok,
                    physical_identity=observed.physical_identity,
                    membership=observed.membership,
                    runtime_revision=observed.runtime_revision,
                    capabilities=create_receipt.capabilities or observed.capabilities,
                    error=observed.error,
                    raw={
                        **create_receipt.raw,
                        "physical_identity": observed.physical_identity,
                        "membership": observed.membership,
                        "runtime_revision": observed.runtime_revision,
                        "read_back": observed.raw,
                    },
                )
        except ZoneRuntimeUnavailable as exc:
            with self._session_factory() as session, session.begin():
                _fenced_update(
                    session,
                    ZoneOperationModel,
                    operation_id,
                    fence=fence,
                    values={
                        "state": "running",
                        "step": "runtime-unknown",
                        "retryable": True,
                        "error": {
                            "code": "ZONE_RUNTIME_UNAVAILABLE",
                            "message": str(exc),
                            "retryable": True,
                        },
                    },
                )
            return OperationResult(operation_id, "running", "runtime-unknown", True)

        with self._session_factory() as session, session.begin():
            zone = session.get(ZoneModel, zone_id)
            assert zone is not None
            updated = _fenced_update(
                session,
                ZoneOperationModel,
                operation_id,
                fence=fence,
                values={"state": "running", "step": "read-back"},
            )
            if not updated:
                # A takeover bumped the fence between our runtime call and
                # this write-back: this worker is stale. Write nothing else
                # in this transaction (the genesis grant and projection
                # outbox included) — the fence-owning worker owns the saga
                # now, and the UPDATE's row lock holds to commit, so later
                # fenced writes cannot race past this check.
                return OperationResult(
                    operation_id, "failed", "stale-worker-fenced", False
                )
            if not receipt.ok or not receipt.physical_identity:
                # A deterministic runtime refusal is terminal and must not be
                # retried (the receipt says so); only unknown-family failures
                # stay retryable.
                retryable = not receipt.rejected
                code = (
                    "ZONE_RUNTIME_REJECTED" if receipt.rejected else "ZONE_RUNTIME_UNAVAILABLE"
                )
                _fenced_update(
                    session,
                    ZoneOperationModel,
                    operation_id,
                    fence=fence,
                    values={
                        "state": "failed",
                        "step": "runtime-create",
                        "retryable": retryable,
                        "error": {
                            "code": code,
                            "message": receipt.error or "runtime refused create",
                            "retryable": retryable,
                        },
                    },
                )
                return OperationResult(operation_id, "failed", "runtime-create", retryable)

            # §5.2.5-7: read back identity/membership/revision, then the
            # mandatory initial grant, then active-with-receipt — one commit.
            grant = ZoneGrantModel(
                grant_id=_new_id("grant"),
                zone_id=zone_id,
                grantee=zone.created_by,
                capabilities=list(GENESIS_CAPABILITIES),
                source_type="system",
                source_id=f"genesis:{zone_id}",
                issued_by={"subject_type": "service", "subject_id": "nexus-zone-service"},
                reason="mandatory initial grant at zone creation",
                policy_version="zone-v1",
                revision=_new_id("rev"),
                status="pending",
            )
            session.add(grant)
            session.flush()
            session.add(
                ZoneGrantProjectionOutboxModel(
                    grant_id=grant.grant_id,
                    event_type="grant.apply_projections",
                    payload={
                        "grant_id": grant.grant_id,
                        "zone_id": zone_id,
                        "operation_id": operation_id,
                    },
                )
            )
            _fenced_update(
                session,
                ZoneOperationModel,
                operation_id,
                fence=fence,
                values={
                    "state": "waiting_dependency",
                    "step": "mandatory-grant-projection",
                    "retryable": True,
                    "grant_id": grant.grant_id,
                    "receipt": receipt.raw,
                },
            )
            return OperationResult(
                operation_id,
                "waiting_dependency",
                "mandatory-grant-projection",
                True,
            )

    # ── grant issuance (§5.3) ────────────────────────────────────────────────

    def issue_grant(
        self,
        zone_id: str,
        request: ZoneGrantCreateRequest,
        *,
        idempotency_key: str,
        principal: dict[str, Any],
    ) -> OperationResult:
        self._require_principal(principal)
        unsupported = sorted(set(request.capabilities) - KNOWN_CAPABILITIES)
        if unsupported:
            raise ServiceError(
                "UNSUPPORTED_CAPABILITY",
                f"unsupported capabilities: {', '.join(unsupported)}",
                http_status=422,
            )
        scope = json.dumps([_scope_subject(principal), "zone.grant", zone_id])
        req_hash = _request_hash(request.model_dump(mode="json"))
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(
                session, scope=scope, key=idempotency_key, req_hash=req_hash
            )
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                raise ServiceError("ZONE_NOT_FOUND", f"zone {zone_id} not found", http_status=404)
            if zone.canonical_status != "active":
                raise ServiceError(
                    "ZONE_NOT_ACTIVE",
                    f"zone {zone_id} is {zone.canonical_status or 'unprovisioned'}; grants need active",
                    http_status=409,
                )
            if request.source is not None:
                dupe = session.execute(
                    select(ZoneGrantModel).where(
                        # same-zone only: a source (e.g. an org binding)
                        # legitimately grants on multiple zones; the replay
                        # must answer with THIS zone's operation, never a
                        # foreign zone's
                        ZoneGrantModel.source_type == request.source.source_type,
                        ZoneGrantModel.source_id == request.source.source_id,
                        ZoneGrantModel.zone_id == zone_id,
                    )
                ).scalar_one_or_none()
                if dupe is not None:
                    existing_op = session.execute(
                        select(ZoneOperationModel).where(
                            ZoneOperationModel.grant_id == dupe.grant_id,
                            ZoneOperationModel.action == "grant",
                        )
                    ).scalar_one_or_none()
                    if existing_op is None:
                        raise ServiceError(
                            "IDEMPOTENCY_CONFLICT",
                            "grant source already exists without a replayable operation",
                            http_status=409,
                        )
                    return OperationResult(
                        existing_op.operation_id,
                        existing_op.state,
                        "idempotent-source-replay",
                        existing_op.retryable,
                    )
            grant = ZoneGrantModel(
                grant_id=_new_id("grant"),
                zone_id=zone_id,
                grantee=request.grantee.model_dump(mode="json"),
                capabilities=request.capabilities,
                resource_prefixes=request.resource_prefixes,
                source_type=request.source.source_type if request.source else "manual",
                source_id=request.source.source_id if request.source else idempotency_key,
                issued_by=principal,
                reason=request.reason,
                policy_version=request.policy_version or "zone-v1",
                revision=_new_id("rev"),
                status="pending",  # §5.3: pending grants no access
                not_before=_parse_ts(request.not_before),
                expires_at=_parse_ts(request.expires_at),
            )
            session.add(grant)
            session.add(
                ZoneGrantProjectionOutboxModel(
                    grant_id=grant.grant_id,
                    event_type="grant.apply_projections",
                    payload={
                        "grant_id": grant.grant_id,
                        "zone_id": zone_id,
                        "capabilities": request.capabilities,
                        "grantee": request.grantee.model_dump(mode="json"),
                        "resource_prefixes": request.resource_prefixes or [],
                    },
                )
            )
            op = self._new_operation(
                session,
                action="grant",
                zone_id=zone_id,
                grant_id=grant.grant_id,
                scope=scope,
                key=idempotency_key,
                req_hash=req_hash,
            )
            operation_id = op.operation_id
            grant_id = grant.grant_id
            result = OperationResult(op.operation_id, op.state, op.step, op.retryable)
            self._audit(
                "zone.grant.accepted",
                principal=principal,
                zone_id=zone_id,
                operation_id=operation_id,
                grant_id=grant_id,
            )
        return result

    def complete_grant_projection(self, *, grant_id: str) -> bool:
        """Mandatory projections done → advance epoch, then active (§5.3).

        Returns False when the grant is gone or already terminal; the outbox
        keeps the event for retry otherwise.
        """
        # Project into the external ReBAC store without holding the canonical
        # SQL transaction.  Some supported deployments use SQLite for both
        # adapters; nesting the ReBAC write under this transaction deadlocks
        # on its second connection.  Projection calls are idempotent, so a
        # crash between this side effect and the canonical commit is retried
        # through the outbox with the same grant provenance.
        with self._session_factory() as session:
            grant = session.get(ZoneGrantModel, grant_id)
            if grant is None or grant.status != "pending":
                return grant is not None
            zone_id = grant.zone_id
            edges = _projection_edges(grant)

        try:
            for subject, relation, resource_path in edges:
                if self._projection_write is not None:
                    self._projection_write(zone_id, subject, relation, resource_path)
        except Exception as exc:
            logger.exception("mandatory grant projection failed for %s", grant_id)
            with self._session_factory() as session, session.begin():
                operation = session.execute(
                    select(ZoneOperationModel).where(ZoneOperationModel.grant_id == grant_id)
                ).scalar_one_or_none()
                if operation is not None:
                    operation.state = "waiting_dependency"
                    operation.step = "grant-projection"
                    operation.retryable = True
                    operation.error = {
                        "code": "PROJECTION_FAILED",
                        "message": str(exc),
                        "retryable": True,
                    }
            return False

        with self._session_factory() as session, session.begin():
            grant = session.get(ZoneGrantModel, grant_id)
            if grant is None or grant.status != "pending":
                # The grant stopped being pending while our external writes
                # were in flight (a concurrent revoke). Those edges may
                # already live in the external store with no accounting rows
                # for the revoker's cleanup to find — enqueue a cleanup event
                # of our own so the deleter recomputes from the grant itself
                # and no edge survives the revoke.
                if grant is not None:
                    session.add(
                        ZoneGrantProjectionOutboxModel(
                            grant_id=grant_id,
                            event_type="grant.cleanup_projections",
                            payload={
                                "grant_id": grant_id,
                                "zone_id": grant.zone_id,
                                "reason": "activation lost the race to a terminal state",
                            },
                        )
                    )
                return grant is not None
            # Final adjudication inside the SAME transaction as the activation
            # write: if the zone entered or completed deprovision while this
            # projection event was pending, the grant dies with the zone — a
            # late projection must never resurrect a deleted zone (H-1) nor
            # activate a pending grant on it.  Returning True marks the outbox
            # event done instead of retrying.
            zone = session.get(ZoneModel, grant.zone_id)
            if zone is not None and zone.canonical_status in ("deleting", "deleted"):
                grant.status = "revoked"
                grant.revoked_at = _now()
                grant.revoke_reason = "zone deprovision superseded grant activation"
                operation = session.execute(
                    select(ZoneOperationModel).where(ZoneOperationModel.grant_id == grant_id)
                ).scalar_one_or_none()
                if operation is not None:
                    operation.state = "failed"
                    operation.retryable = False
                    operation.error = {
                        "code": "PROJECTION_FAILED",
                        "message": "zone deprovision superseded grant activation",
                        "retryable": False,
                    }
                    operation.completed_at = _now()
                return True
            for subject, relation, resource_path in edges:
                existing = session.execute(
                    select(RebacRelationSourceModel).where(
                        RebacRelationSourceModel.subject == _subject_key(subject),
                        RebacRelationSourceModel.relation == relation,
                        RebacRelationSourceModel.object == resource_path,
                        RebacRelationSourceModel.source_grant_id == grant.grant_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    session.add(
                        RebacRelationSourceModel(
                            zone_id=grant.zone_id,
                            subject=_subject_key(subject),
                            relation=relation,
                            object=resource_path,
                            source_grant_id=grant.grant_id,
                            reference_state="active",
                        )
                    )
                else:
                    existing.reference_state = "active"
                    existing.updated_at = _now()
            _advance_epoch(session, grant.zone_id, f"grant {grant_id} activated")
            grant.status = "active"
            operation = session.execute(
                select(ZoneOperationModel).where(ZoneOperationModel.grant_id == grant_id)
            ).scalar_one_or_none()
            if operation is not None:
                operation.state = "succeeded"
                operation.step = (
                    "mark-active-with-receipt"
                    if operation.action == "create"
                    else "grant-projections-active"
                )
                operation.retryable = False
                operation.error = None
                operation.completed_at = _now()
                if operation.action == "create" and operation.zone_id is not None:
                    zone = session.get(ZoneModel, operation.zone_id)
                    assert zone is not None
                    zone.canonical_status = "active"
                    zone.phase = "Active"
                    zone.runtime_observed_receipt = operation.receipt
                    zone.runtime_health = "healthy"
                    zone.runtime_observed_at = _now()
                    zone.canonical_revision = _new_id("rev")
                    self._audit(
                        "zone.create.completed",
                        principal=zone.created_by or {},
                        zone_id=zone.zone_id,
                        operation_id=operation.operation_id,
                    )
            self._audit(
                "zone.grant.completed",
                principal=grant.issued_by,
                zone_id=grant.zone_id,
                grant_id=grant_id,
            )
            return True

    # ── revoke (§5.5) ────────────────────────────────────────────────────────

    def revoke_grant(
        self,
        zone_id: str,
        grant_id: str,
        *,
        principal: dict[str, Any],
        reason: str,
        idempotency_key: str | None = None,
    ) -> OperationResult:
        self._require_principal(principal)
        scope = json.dumps([_scope_subject(principal), "zone.grant.revoke", grant_id])
        key = idempotency_key or grant_id
        req_hash = _request_hash({"grant_id": grant_id, "reason": reason})
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(session, scope=scope, key=key, req_hash=req_hash)
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            grant = session.get(ZoneGrantModel, grant_id)
            if grant is None or grant.zone_id != zone_id:
                raise ServiceError(
                    "GRANT_NOT_FOUND", f"grant {grant_id} not found on {zone_id}", http_status=404
                )
            if grant.status == "revoked":
                raise ServiceError("GRANT_REVOKED", f"grant {grant_id} is revoked", http_status=409)
            # ONE transaction: revoked fact + epoch + invalidation outbox.
            grant.status = "revoked"
            grant.revoked_at = _now()
            grant.revoked_by = principal
            grant.revoke_reason = reason
            authorization_revision = _new_id("rev")
            # The new revision is a fact of this revoke (§6.3 promises it in
            # the response): persist it on the grant row itself, not only in
            # the outbox payload, so readers observe what they were told.
            grant.revision = authorization_revision
            new_epoch = _advance_epoch(session, zone_id, f"grant {grant_id} revoked")
            session.add(
                ZoneGrantProjectionOutboxModel(
                    grant_id=grant_id,
                    event_type="grant.cleanup_projections",
                    payload={
                        "grant_id": grant_id,
                        "zone_id": zone_id,
                        "authorization_revision": authorization_revision,
                        "authorization_epoch": new_epoch,
                    },
                )
            )
            op = self._new_operation(
                session,
                action="revoke",
                zone_id=zone_id,
                grant_id=grant_id,
                scope=scope,
                key=key,
                req_hash=req_hash,
            )
            op.state = "succeeded"
            op.step = "revoked-epoch-committed"
            op.retryable = False
            op.completed_at = _now()
            self._audit(
                "zone.grant.revoked",
                principal=principal,
                zone_id=zone_id,
                operation_id=op.operation_id,
                grant_id=grant_id,
            )
            return OperationResult(op.operation_id, op.state, op.step, False, zone_status=None)

    def cleanup_grant_projection(self, *, grant_id: str) -> int:
        """Remove only this grant's derived edges (provenance-clean)."""
        with self._session_factory() as session, session.begin():
            grant = session.get(ZoneGrantModel, grant_id)
            if grant is None:
                return 0
            zone_id = grant.zone_id
            rows = (
                session.execute(
                    select(RebacRelationSourceModel).where(
                        RebacRelationSourceModel.source_grant_id == grant_id,
                    )
                )
                .scalars()
                .all()
            )
            edges = [(edge.subject, edge.relation, edge.object) for edge in rows]
            removed = sum(edge.reference_state == "active" for edge in rows)
            for edge in rows:
                edge.reference_state = "removed"
                edge.updated_at = _now()
            if not rows:
                # Racing activation path: the grant's edges may already be in
                # the external store while no accounting row was written (the
                # activation's final transaction lost the race to the revoke).
                # Recompute the edge set from the grant itself so the external
                # delete below still covers every edge this grant produced.
                edges = [
                    (_subject_key(subject), relation, resource_path)
                    for subject, relation, resource_path in _projection_edges(grant)
                ]

        # The external ReBAC adapter can use the same SQLite database.  Never
        # call it while the provenance transaction owns a write lock.  Include
        # already-removed rows above so a crash after the canonical commit can
        # safely retry this materialization cleanup.
        for subject, relation, resource_path in dict.fromkeys(edges):
            with self._session_factory() as session:
                remaining = session.execute(
                    select(RebacRelationSourceModel.id).where(
                        # same-zone only: a foreign zone's projection of the
                        # same (subject, relation, object) must never keep
                        # this zone's edge alive (and vice versa)
                        RebacRelationSourceModel.zone_id == zone_id,
                        RebacRelationSourceModel.subject == subject,
                        RebacRelationSourceModel.relation == relation,
                        RebacRelationSourceModel.object == resource_path,
                        RebacRelationSourceModel.reference_state == "active",
                    )
                ).first()
            if remaining is None and self._projection_delete is not None:
                self._projection_delete(
                    zone_id,
                    {
                        "subject_type": subject.split(":", 1)[0],
                        "subject_id": subject.split(":", 1)[-1],
                    },
                    relation,
                    resource_path,
                )
        return removed

    # ── lifecycle transitions (§4.5 / §5.6 entry) ────────────────────────────

    def suspend_zone(
        self,
        zone_id: str,
        *,
        principal: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> OperationResult:
        return self._lifecycle(
            zone_id,
            "suspend",
            principal,
            required_status=("active",),
            idempotency_key=idempotency_key,
        )

    def resume_zone(
        self,
        zone_id: str,
        *,
        principal: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> OperationResult:
        return self._lifecycle(
            zone_id,
            "resume",
            principal,
            required_status=("suspended",),
            idempotency_key=idempotency_key,
        )

    def _lifecycle(
        self,
        zone_id: str,
        action: str,
        principal: dict[str, Any],
        *,
        required_status: tuple[str, ...],
        idempotency_key: str | None,
    ) -> OperationResult:
        self._require_principal(principal)
        scope = json.dumps([_scope_subject(principal), f"zone.{action}", zone_id])
        key = idempotency_key or f"{action}:{zone_id}"
        req_hash = _request_hash({"action": action, "zone_id": zone_id})
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(session, scope=scope, key=key, req_hash=req_hash)
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                raise ServiceError("ZONE_NOT_FOUND", f"zone {zone_id} not found", http_status=404)
            if zone_id in ("root", "__control__"):
                raise ServiceError(
                    "RESERVED_ZONE_ID",
                    "root/control zones cannot change lifecycle",
                    http_status=403,
                )
            if zone.canonical_status not in required_status:
                raise ServiceError(
                    "ZONE_NOT_ACTIVE",
                    f"{action} requires {required_status}, zone is {zone.canonical_status}",
                    http_status=409,
                )
            zone.canonical_status = "suspended" if action == "suspend" else "active"
            zone.canonical_revision = _new_id("rev")
            op = self._new_operation(
                session,
                action=action,
                zone_id=zone_id,
                scope=scope,
                key=key,
                req_hash=req_hash,
            )
            op.state = "succeeded"
            op.step = "status-transitioned"
            op.retryable = False
            op.completed_at = _now()
            return OperationResult(op.operation_id, op.state, op.step, False, zone.canonical_status)

    def patch_zone(
        self,
        zone_id: str,
        request: ZonePatchRequest,
        *,
        revision_if_match: str | None,
        idempotency_key: str | None = None,
        principal: dict[str, Any] | None = None,
    ) -> str:
        """Whitelist patch; returns the new revision (§6.2)."""
        principal = principal or {"subject_type": "service", "subject_id": "internal"}
        self._require_principal(principal)
        scope = json.dumps([_scope_subject(principal), "zone.patch", zone_id])
        key = idempotency_key or f"patch:{zone_id}:{revision_if_match or 'unconditional'}"
        req_hash = _request_hash(request.model_dump(mode="json"))
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(session, scope=scope, key=key, req_hash=req_hash)
            if replay is not None and replay.revision:
                return str(replay.revision)
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                raise ServiceError("ZONE_NOT_FOUND", f"zone {zone_id} not found", http_status=404)
            if zone.canonical_status in ("deleting", "deleted"):
                # a zone past its delete decision no longer takes metadata
                # edits — the audit trail must not wobble after the fact
                raise ServiceError(
                    "ZONE_NOT_ACTIVE",
                    f"zone {zone_id} is {zone.canonical_status}; patching is closed",
                    http_status=409,
                )
            current = zone.canonical_revision or ""
            if revision_if_match is not None and revision_if_match != current:
                raise ServiceError(
                    "ZONE_REVISION_CONFLICT", "If-Match revision is stale", http_status=412
                )
            if request.display_name is not None:
                zone.display_name = request.display_name
                zone.name = request.display_name
            if request.description is not None:
                zone.description = request.description
            if request.labels is not None:
                zone.labels = request.labels
            if request.deployment is not None:
                if request.deployment.region is not None:
                    zone.placement_region = request.deployment.region
                if request.deployment.data_domain is not None:
                    zone.placement_data_domain = request.deployment.data_domain
            zone.canonical_revision = _new_id("rev")
            op = self._new_operation(
                session,
                action="patch",
                zone_id=zone_id,
                scope=scope,
                key=key,
                req_hash=req_hash,
            )
            op.state = "succeeded"
            op.step = "metadata-updated"
            op.retryable = False
            op.revision = zone.canonical_revision
            op.completed_at = _now()
            return str(zone.canonical_revision)

    # ── operator runtime operations (§6.5) ──────────────────────────────────

    def request_join(
        self,
        zone_id: str,
        *,
        peers: list[str],
        learner: bool,
        idempotency_key: str,
        principal: dict[str, Any],
    ) -> OperationResult:
        self._require_principal(principal)
        return self._queue_runtime_operation(
            action="create",
            event_type="zone.join",
            zone_id=zone_id,
            idempotency_key=idempotency_key,
            principal=principal,
            payload={"zone_id": zone_id, "peers": sorted(peers), "learner": learner},
        )

    def request_mount(
        self,
        *,
        parent_zone_id: str,
        target_zone_id: str,
        path: str,
        idempotency_key: str,
        principal: dict[str, Any],
    ) -> OperationResult:
        self._require_principal(principal)
        with self._session_factory() as session, session.begin():
            for zone_id in (parent_zone_id, target_zone_id):
                zone = session.get(ZoneModel, zone_id)
                if zone is None:
                    raise ServiceError(
                        "ZONE_NOT_FOUND", f"zone {zone_id} not found", http_status=404
                    )
                if zone.canonical_status != "active":
                    raise ServiceError(
                        "ZONE_NOT_ACTIVE",
                        f"zone {zone_id} is not active",
                        http_status=409,
                    )
            scope = json.dumps(
                [principal.get("subject_id"), "zone.mount", parent_zone_id, target_zone_id, path]
            )
            payload = {
                "parent_zone_id": parent_zone_id,
                "target_zone_id": target_zone_id,
                "path": path,
            }
            req_hash = _request_hash(payload)
            replay = self._claim_idempotency(
                session, scope=scope, key=idempotency_key, req_hash=req_hash
            )
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            mount = (
                session.execute(
                    select(ZoneMountModel).where(
                        ZoneMountModel.parent_zone_id == parent_zone_id,
                        ZoneMountModel.target_zone_id == target_zone_id,
                        ZoneMountModel.path == path,
                    )
                )
                .scalars()
                .one_or_none()
            )
            if mount is None:
                mount = ZoneMountModel(
                    mount_id=_new_id("mount"),
                    parent_zone_id=parent_zone_id,
                    target_zone_id=target_zone_id,
                    path=path,
                    desired_state="mounted",
                    observed_state=None,
                )
                session.add(mount)
            else:
                # mount → unmount → remount of the same triple is a legal
                # operations cycle: revive the unmounted row instead of
                # tripping uq_zone_mount with an unconditional INSERT
                mount.desired_state = "mounted"
                mount.observed_state = None
            op = self._new_operation(
                session,
                action="mount",
                zone_id=parent_zone_id,
                scope=scope,
                key=idempotency_key,
                req_hash=req_hash,
            )
            mount_id = mount.mount_id
            op.result = {"mount_id": mount_id}
            session.add(
                ZoneRuntimeOutboxModel(
                    operation_id=op.operation_id,
                    event_type="zone.mount",
                    payload={**payload, "mount_id": mount_id},
                )
            )
            result = OperationResult(op.operation_id, op.state, op.step, op.retryable)
        if self._worker_enabled:
            return self.process_runtime_operation(
                operation_id=result.operation_id,
                event_type="zone.mount",
                payload={**payload, "mount_id": mount_id},
            )
        return result

    def request_unmount(
        self,
        mount_id: str,
        *,
        idempotency_key: str,
        principal: dict[str, Any],
    ) -> OperationResult:
        self._require_principal(principal)
        with self._session_factory() as session, session.begin():
            mount = session.get(ZoneMountModel, mount_id)
            if mount is None:
                raise ServiceError("ZONE_NOT_FOUND", f"mount {mount_id} not found", http_status=404)
            scope = json.dumps([_scope_subject(principal), "zone.unmount", mount_id])
            payload = {
                "mount_id": mount.mount_id,
                "parent_zone_id": mount.parent_zone_id,
                "target_zone_id": mount.target_zone_id,
                "path": mount.path,
            }
            req_hash = _request_hash(payload)
            replay = self._claim_idempotency(
                session, scope=scope, key=idempotency_key, req_hash=req_hash
            )
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            mount.desired_state = "unmounted"
            op = self._new_operation(
                session,
                action="unmount",
                zone_id=mount.parent_zone_id,
                scope=scope,
                key=idempotency_key,
                req_hash=req_hash,
            )
            op.result = {"mount_id": mount.mount_id}
            session.add(
                ZoneRuntimeOutboxModel(
                    operation_id=op.operation_id,
                    event_type="zone.unmount",
                    payload=payload,
                )
            )
            result = OperationResult(op.operation_id, op.state, op.step, op.retryable)
        if self._worker_enabled:
            return self.process_runtime_operation(
                operation_id=result.operation_id,
                event_type="zone.unmount",
                payload=payload,
            )
        return result

    def request_transfer(
        self,
        *,
        source: dict[str, Any],
        target: dict[str, Any],
        idempotency_key: str,
        principal: dict[str, Any],
    ) -> OperationResult:
        """Execute only through an injected egress/trust policy and copier.

        A deployment without either provider returns an explicit capability
        error; it never falls back to a raw VFS copy.
        """
        self._require_principal(principal)
        if self._transfer_policy is None or self._transfer_executor is None:
            raise ServiceError(
                "UNSUPPORTED_CAPABILITY",
                "cross-zone transfer policy/executor is not armed",
                http_status=501,
            )
        if not self._transfer_policy(source, target, principal):
            raise ServiceError(
                "RESOURCE_RELATION_DENIED",
                "cross-zone egress/trust policy denied transfer",
                http_status=403,
            )
        scope = json.dumps(
            [
                principal.get("subject_id"),
                "zone.transfer",
                source.get("zone_id"),
                target.get("zone_id"),
            ]
        )
        req_hash = _request_hash({"source": source, "target": target})
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(
                session, scope=scope, key=idempotency_key, req_hash=req_hash
            )
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            op = self._new_operation(
                session,
                action="transfer",
                zone_id=str(source.get("zone_id") or ""),
                scope=scope,
                key=idempotency_key,
                req_hash=req_hash,
            )
            operation_id = op.operation_id
        try:
            result_payload = self._transfer_executor(source, target, operation_id)
        except Exception as exc:
            with self._session_factory() as session, session.begin():
                failed_operation = session.get(ZoneOperationModel, operation_id)
                assert failed_operation is not None
                failed_operation.state = "failed"
                failed_operation.step = "transfer"
                # Transfers are driven inline only — no outbox row is ever
                # queued for one, so no system path retries this operation.
                # Marking it retryable would advertise a retry that never
                # comes; the caller retries by re-issuing with its own
                # idempotency key.
                failed_operation.retryable = False
                failed_operation.error = {
                    "code": "ZONE_RUNTIME_UNAVAILABLE",
                    "message": str(exc),
                    "retryable": False,
                }
            return OperationResult(operation_id, "failed", "transfer", False)
        with self._session_factory() as session, session.begin():
            completed_operation = session.get(ZoneOperationModel, operation_id)
            assert completed_operation is not None
            completed_operation.state = "succeeded"
            completed_operation.step = "transfer-completed"
            completed_operation.retryable = False
            completed_operation.result = result_payload
            completed_operation.completed_at = _now()
        self._audit(
            "zone.transfer.completed",
            principal=principal,
            zone_id=str(source.get("zone_id") or ""),
            operation_id=operation_id,
        )
        return OperationResult(operation_id, "succeeded", "transfer-completed", False)

    def _queue_runtime_operation(
        self,
        *,
        action: str,
        event_type: str,
        zone_id: str,
        idempotency_key: str,
        principal: dict[str, Any],
        payload: dict[str, Any],
    ) -> OperationResult:
        with self._session_factory() as session, session.begin():
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                raise ServiceError("ZONE_NOT_FOUND", f"zone {zone_id} not found", http_status=404)
            if zone.canonical_status not in ("active", "suspended"):
                raise ServiceError(
                    "ZONE_NOT_ACTIVE", f"zone {zone_id} is not usable", http_status=409
                )
            scope = json.dumps([_scope_subject(principal), event_type, zone_id])
            req_hash = _request_hash(payload)
            replay = self._claim_idempotency(
                session, scope=scope, key=idempotency_key, req_hash=req_hash
            )
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            op = self._new_operation(
                session,
                action=action,
                zone_id=zone_id,
                scope=scope,
                key=idempotency_key,
                req_hash=req_hash,
            )
            op.step = f"{event_type}.accepted"
            op.result = {"runtime_event": event_type, "request": payload}
            session.add(
                ZoneRuntimeOutboxModel(
                    operation_id=op.operation_id,
                    event_type=event_type,
                    payload=payload,
                )
            )
            result = OperationResult(op.operation_id, op.state, op.step, op.retryable)
        return result

    # ── deprovision saga entry (§5.6) ────────────────────────────────────────

    def request_deprovision(
        self,
        zone_id: str,
        *,
        principal: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> OperationResult:
        self._require_principal(principal)
        scope = json.dumps([_scope_subject(principal), "zone.deprovision", zone_id])
        key = idempotency_key or f"deprovision:{zone_id}"
        req_hash = _request_hash({"zone_id": zone_id})
        with self._session_factory() as session, session.begin():
            replay = self._claim_idempotency(session, scope=scope, key=key, req_hash=req_hash)
            if replay is not None:
                return OperationResult(
                    replay.operation_id, replay.state, replay.step, replay.retryable
                )
            zone = session.get(ZoneModel, zone_id)
            if zone is None:
                raise ServiceError("ZONE_NOT_FOUND", f"zone {zone_id} not found", http_status=404)
            if zone_id in ("root", "__control__"):
                raise ServiceError(
                    "RESERVED_ZONE_ID", "root/control zones cannot deprovision", http_status=403
                )
            blockers: list[str] = []
            active_grants = (
                session.execute(
                    select(ZoneGrantModel).where(
                        ZoneGrantModel.zone_id == zone_id,
                        ZoneGrantModel.status == "active",
                        ZoneGrantModel.source_type
                        != "system",  # genesis/system grants die with the zone (§5.6)
                    )
                )
                .scalars()
                .all()
            )
            if active_grants:
                blockers.append(f"{len(active_grants)} active grant(s) must be revoked first")
            active_mounts = (
                session.execute(
                    select(ZoneMountModel).where(
                        (ZoneMountModel.parent_zone_id == zone_id)
                        | (ZoneMountModel.target_zone_id == zone_id),
                        ZoneMountModel.desired_state == "mounted",
                    )
                )
                .scalars()
                .all()
            )
            if active_mounts:
                blockers.append(f"{len(active_mounts)} active mount(s) must be removed first")
            active_states = ("registered", "warming_up", "ready", "busy", "awaiting_input")
            active_runs = (
                session.execute(
                    select(SessionRuntimeRunModel)
                    .join(
                        SessionModel, SessionModel.session_id == SessionRuntimeRunModel.session_id
                    )
                    .where(
                        (
                            (SessionRuntimeRunModel.execution_zone_id == zone_id)
                            | (SessionModel.home_zone_id == zone_id)
                        ),
                        SessionRuntimeRunModel.state.in_(active_states),
                    )
                )
                .scalars()
                .all()
            )
            if active_runs:
                blockers.append(f"{len(active_runs)} active runtime(s) must be cancelled first")
            if blockers:
                # Blocker identities alongside the count-only message: admins
                # must be able to act (revoke this grant, cancel this run)
                # without spelunking the store. Message wording stays stable
                # for older clients; details is additive.
                raise ServiceError(
                    "ZONE_DELETE_BLOCKED",
                    "; ".join(blockers),
                    http_status=409,
                    details={
                        "grants": [
                            {"grant_id": g.grant_id, "grantee": g.grantee} for g in active_grants
                        ],
                        "runs": [
                            {"pid": r.pid, "session_id": r.session_id, "state": r.state}
                            for r in active_runs
                        ],
                        "mounts": [{"mount_id": m.mount_id} for m in active_mounts],
                    },
                )
            # Advance the epoch BEFORE revoking so the cleanup events carry the
            # post-invalidation epoch (park_runs_for_revocation compares
            # against it); same transaction, no concurrency window.
            deletion_epoch = _advance_epoch(session, zone_id, "zone deprovision requested")
            # §5.6: revoke the zone's own grants as part of the flow.
            for g in (
                session.execute(
                    select(ZoneGrantModel).where(
                        ZoneGrantModel.zone_id == zone_id, ZoneGrantModel.status == "active"
                    )
                )
                .scalars()
                .all()
            ):
                g.status = "revoked"
                g.revoked_at = _now()
                g.revoked_by = principal
                g.revoke_reason = "zone deprovision"
                session.add(
                    ZoneGrantProjectionOutboxModel(
                        grant_id=g.grant_id,
                        event_type="grant.cleanup_projections",
                        payload={
                            "grant_id": g.grant_id,
                            "zone_id": zone_id,
                            "authorization_epoch": deletion_epoch,
                        },
                    )
                )
            # Pending grants die with the zone too: their apply_projections
            # outbox events would otherwise activate them (and, for the
            # genesis grant, resurrect the zone) once the projection worker
            # catches up after deletion.
            for g in (
                session.execute(
                    select(ZoneGrantModel).where(
                        ZoneGrantModel.zone_id == zone_id, ZoneGrantModel.status == "pending"
                    )
                )
                .scalars()
                .all()
            ):
                g.status = "revoked"
                g.revoked_at = _now()
                g.revoked_by = principal
                g.revoke_reason = "zone deprovision superseded grant activation"
                session.execute(
                    update(ZoneGrantProjectionOutboxModel)
                    .where(
                        ZoneGrantProjectionOutboxModel.grant_id == g.grant_id,
                        ZoneGrantProjectionOutboxModel.event_type == "grant.apply_projections",
                        ZoneGrantProjectionOutboxModel.processed_at.is_(None),
                    )
                    .values(processed_at=_now())
                )
                pending_op = session.execute(
                    select(ZoneOperationModel).where(
                        ZoneOperationModel.grant_id == g.grant_id,
                        ZoneOperationModel.state.in_(("queued", "running", "waiting_dependency")),
                    )
                ).scalar_one_or_none()
                if pending_op is not None:
                    pending_op.state = "failed"
                    pending_op.retryable = False
                    pending_op.error = {
                        "code": "PROJECTION_FAILED",
                        "message": "zone deprovision superseded grant activation",
                        "retryable": False,
                    }
                    pending_op.completed_at = _now()
            zone.canonical_status = "deleting"
            zone.canonical_revision = _new_id("rev")
            op = self._new_operation(
                session,
                action="deprovision",
                zone_id=zone_id,
                scope=scope,
                key=key,
                req_hash=req_hash,
            )
            session.add(
                ZoneRuntimeOutboxModel(
                    operation_id=op.operation_id,
                    event_type="zone.deprovision",
                    payload={"zone_id": zone_id, "deletion_epoch": deletion_epoch},
                )
            )
            result = OperationResult(op.operation_id, op.state, op.step, op.retryable)
            self._audit(
                "zone.deprovision.accepted",
                principal=principal,
                zone_id=zone_id,
                operation_id=op.operation_id,
            )
        return result

    def process_runtime_operation(
        self,
        *,
        operation_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> OperationResult:
        """Execute one typed runtime effect and persist only read-back facts."""
        with self._session_factory() as session, session.begin():
            op = session.get(ZoneOperationModel, operation_id)
            if op is None:
                return OperationResult(operation_id, "failed", "operation-missing", False)
            if op.state in ("succeeded", "failed"):
                return OperationResult(op.operation_id, op.state, op.step, op.retryable)
            op.state = "running"
            op.step = event_type
            op.lease_owner = f"runtime-{secrets.token_hex(4)}"
            op.generation += 1
            op.fence += 1
            fence = int(op.fence)

        ctx = {"operation_id": operation_id}
        try:
            if event_type == "zone.join":
                receipt = self._runtime.join_zone(
                    zone_id=str(payload["zone_id"]),
                    peers=list(payload.get("peers") or ()),
                    ctx={**ctx, "learner": bool(payload.get("learner", False))},
                )
            elif event_type == "zone.mount":
                receipt = self._runtime.mount(
                    parent_zone_id=str(payload["parent_zone_id"]),
                    target_zone_id=str(payload["target_zone_id"]),
                    path=str(payload["path"]),
                    ctx=ctx,
                )
            elif event_type == "zone.unmount":
                receipt = self._runtime.unmount(
                    ctx={
                        **ctx,
                        "parent_zone_id": payload["parent_zone_id"],
                        "path": payload["path"],
                    },
                )
            elif event_type == "zone.deprovision":
                receipt = self._runtime.deprovision(
                    zone_id=str(payload["zone_id"]),
                    ctx=ctx,
                )
            else:
                raise ServiceError(
                    "UNSUPPORTED_CAPABILITY",
                    f"unsupported runtime event {event_type}",
                    http_status=501,
                )
        except ZoneRuntimeUnavailable as exc:
            recover = getattr(self._runtime, "get_operation", None)
            if recover is None:
                return self._record_runtime_unknown(operation_id, fence, event_type, str(exc))
            try:
                receipt = recover(operation_id=operation_id, ctx=ctx)
            except ZoneRuntimeUnavailable:
                return self._record_runtime_unknown(operation_id, fence, event_type, str(exc))

        if not receipt.ok:
            return self._record_runtime_failure(
                operation_id,
                fence,
                event_type,
                receipt.error or "runtime rejected operation",
                rejected=bool(getattr(receipt, "rejected", False)),
            )

        # §5.6 physical replica teardown: the VFS materialized tree goes
        # FIRST (external effect, idempotent — a crash between this and the
        # SQL commit below is retried by the outbox and the purger re-runs
        # harmlessly), then the SQL rows purge in the same transaction as
        # the operation's terminal state, so a failed purge can never be
        # recorded as succeeded.
        if event_type == "zone.deprovision" and self._zone_fs_purger is not None:
            self._zone_fs_purger(str(payload["zone_id"]))

        with self._session_factory() as session, session.begin():
            updated = _fenced_update(
                session,
                ZoneOperationModel,
                operation_id,
                fence=fence,
                values={
                    "state": "succeeded",
                    "step": f"{event_type}.read-back",
                    "retryable": False,
                    "receipt": receipt.raw,
                    "completed_at": _now(),
                },
            )
            if not updated:
                return OperationResult(operation_id, "running", "stale-worker-fenced", True)
            if event_type in ("zone.mount", "zone.unmount"):
                mount = session.get(ZoneMountModel, str(payload["mount_id"]))
                if mount is not None:
                    mount.observed_state = "mounted" if event_type == "zone.mount" else "unmounted"
                    mount.runtime_revision = receipt.runtime_revision
                    mount.updated_at = _now()
            elif event_type == "zone.deprovision":
                zone = session.get(ZoneModel, str(payload["zone_id"]))
                if zone is not None:
                    _cleanup_zone_projections(session, zone.zone_id)
                    _purge_zone_session_data(session, zone.zone_id)
                    zone.canonical_status = "deleted"
                    zone.phase = "Terminated"
                    zone.deleted_at = _now()
                    zone.runtime_observed_receipt = receipt.raw
                    zone.runtime_health = "deleted"
                    zone.runtime_observed_at = _now()
                    zone.canonical_revision = _new_id("rev")
                    self._audit(
                        "zone.deprovision.completed",
                        principal=zone.created_by or {},
                        zone_id=zone.zone_id,
                        operation_id=operation_id,
                    )
        if event_type == "zone.deprovision" and self._rebac_invalidate is not None:
            # After the purge transaction commits: the raw tuple deletes
            # above never bumped a revision, so cached decisions are
            # stale until this flush runs.
            self._rebac_invalidate()
        return OperationResult(operation_id, "succeeded", f"{event_type}.read-back", False)

    def _record_runtime_unknown(
        self, operation_id: str, fence: int, step: str, message: str
    ) -> OperationResult:
        with self._session_factory() as session, session.begin():
            _fenced_update(
                session,
                ZoneOperationModel,
                operation_id,
                fence=fence,
                values={
                    "state": "running",
                    "step": f"{step}.unknown",
                    "retryable": True,
                    "error": {
                        "code": "ZONE_RUNTIME_UNAVAILABLE",
                        "message": message,
                        "retryable": True,
                    },
                },
            )
        return OperationResult(operation_id, "running", f"{step}.unknown", True)

    def _record_runtime_failure(
        self, operation_id: str, fence: int, step: str, message: str, *, rejected: bool = False
    ) -> OperationResult:
        """Terminal failure write-back; the RETURN VALUE must mirror the
        persisted retryable flag — the worker's outbox short-circuit keys off
        ``state == "failed" and not retryable``."""
        retryable = not rejected
        code = "ZONE_RUNTIME_REJECTED" if rejected else "ZONE_RUNTIME_UNAVAILABLE"
        with self._session_factory() as session, session.begin():
            _fenced_update(
                session,
                ZoneOperationModel,
                operation_id,
                fence=fence,
                values={
                    "state": "failed",
                    "step": step,
                    "retryable": retryable,
                    "error": {
                        "code": code,
                        "message": message,
                        "retryable": retryable,
                    },
                },
            )
        return OperationResult(operation_id, "failed", step, retryable)

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            op = session.get(ZoneOperationModel, operation_id)
            if op is None:
                return None
            try:
                scope = json.loads(op.idempotency_scope)
                principal_id = scope[0] if isinstance(scope, list) and scope else None
            except (TypeError, ValueError, json.JSONDecodeError):
                principal_id = None
            return {
                "operation_id": op.operation_id,
                "action": op.action,
                "zone_id": op.zone_id,
                "grant_id": op.grant_id,
                "state": op.state,
                "step": op.step,
                "retryable": op.retryable,
                "error": op.error,
                "created_at": op.created_at,
                "updated_at": op.updated_at,
                "completed_at": op.completed_at,
                "principal_id": principal_id,
            }


def _advance_epoch(session: Session, zone_id: str, reason: str) -> int:
    """Atomically advance a zone's authorization epoch.

    ``UPDATE ... SET epoch = epoch + 1`` instead of an ORM read-modify-write:
    concurrent activation/revocation/deprovision transactions must never
    merge two advances into one — each advance invalidates every outstanding
    delegation bound to the previous epoch, so a lost increment would let a
    revoked grant keep passing ``epoch_is_current`` until its delegation TTL
    expires. The UPDATE's row lock holds until commit, keeping dependent
    writes in the same transaction safe.
    """
    new_epoch = session.execute(
        update(ZoneAuthorizationEpochModel)
        .where(ZoneAuthorizationEpochModel.zone_id == zone_id)
        .values(
            epoch=ZoneAuthorizationEpochModel.epoch + 1,
            advanced_at=_now(),
            reason=reason,
        )
        .returning(ZoneAuthorizationEpochModel.epoch)
    ).scalar_one_or_none()
    assert new_epoch is not None, f"zone_authorization_epochs row missing for {zone_id}"
    return int(new_epoch)


def _fenced_update(
    session: Session,
    model: Any,
    row_id: str,
    *,
    fence: int | None,
    values: dict[str, Any],
    key_name: str = "operation_id",
) -> int:
    """UPDATE ... WHERE <key> AND (fence = :fence when given): a stale writer
    matches zero rows and changes nothing (§5.7 worker write-back rule)."""
    stmt = update(model).where(getattr(model, key_name) == row_id)
    if fence is not None:
        stmt = stmt.where(model.fence == fence)
    stmt = (
        stmt.values(**values, updated_at=_now())
        if hasattr(model, "updated_at")
        else stmt.values(**values)
    )
    result = session.execute(stmt.execution_options(synchronize_session="fetch"))
    return int(getattr(result, "rowcount", 0) or 0)
