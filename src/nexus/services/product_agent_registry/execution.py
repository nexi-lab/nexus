"""Durable task bindings over real Nexus IAM; all authorization reads primary storage."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC
from uuid import uuid4

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError

from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.bricks.delegation.models import DelegationMode, DelegationScope, DelegationStatus
from nexus.contracts.agent_execution import (
    CheckExecutionGrant,
    ExecutionAuthorization,
    ExecutionGrant,
    ExecutionGrantPage,
    ExecutionGrantResult,
    ExecutionZonePolicy,
    IssueExecutionGrant,
)
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError, digest

metadata = MetaData()
grants = Table(
    "product_agent_execution_grants",
    metadata,
    Column("id", String(150), primary_key=True),
    Column("zone", String(255), nullable=False),
    Column("issuer", String(200), nullable=False),
    Column("asset_id", String(150), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "issuer", "request_id"),
)


def initialize_execution_schema(engine):
    """Explicit additive schema preparation; never called by startup or GET."""
    metadata.create_all(engine)


def permissions(values):
    return {(p.operation, p.resource) for p in values}


class AgentExecution:
    def __init__(self, registry, record_store, delegation, entity_registry, policies):
        if not set(metadata.tables).issubset(inspect(registry.engine).get_table_names()):
            raise RegistryError("EXECUTION_SCHEMA_REQUIRED", 503)
        self.registry = registry
        self.engine = registry.engine
        self.delegation = delegation
        self.entities = entity_registry
        self.auth = DatabaseAPIKeyAuth(record_store, require_expiry=True)
        self.policies = {
            zone: ExecutionZonePolicy.model_validate(p) for zone, p in policies.items()
        }
        if not set(self.policies).issubset(set(registry.domain_zones.values())):
            raise ValueError("EXECUTION_POLICY_ZONE_INVALID")

    def _policy(self, actor, role):
        policy = self.policies.get(actor.zone)
        if not policy or not actor.is_admin or actor.subject not in getattr(policy, role):
            raise RegistryError("EXECUTION_FORBIDDEN", 403)
        # Provisioning is explicit. Config alone cannot invent an IAM coordinator.
        entity = self.entities.get_entity("agent", policy.coordinator_id)
        details = json.loads(entity.entity_metadata or "{}") if entity else {}
        if (
            not entity
            or entity.parent_type != "user"
            or entity.parent_id != policy.coordinator_owner_id
            or details.get("zone_id") != actor.zone
        ):
            raise RegistryError("EXECUTION_COORDINATOR_INVALID", 503)
        return policy

    def _admit(self, actor, request):
        policy = self._policy(actor, "evaluators" if request.mode == "evaluation" else "issuers")
        if request.max_model_calls > policy.max_model_calls:
            raise RegistryError("EXECUTION_CALL_LIMIT_DENIED", 403)
        asset = self.registry.inventory.get(actor, request.asset_id)
        if (
            asset.status != "approved"
            or not asset.metadata.owner_user_id
            or asset.metadata.hosting == "public_saas"
            or asset.metadata_digest != request.metadata_digest
        ):
            raise RegistryError("EXECUTION_ASSET_NOT_ADMITTED")
        allowed = {
            *(("model.invoke", p) for p in asset.metadata.model_paths),
            *(("tool.invoke", p) for p in asset.metadata.tool_refs),
            *(
                (f"data.{op}", p.reference)
                for p in asset.metadata.data_connections
                for op in p.actions
            ),
        }
        deployment = (
            policy.evaluation_permissions if request.mode == "evaluation" else policy.permissions
        )
        if not permissions(request.permissions).issubset(allowed & permissions(deployment)):
            raise RegistryError("EXECUTION_SCOPE_DENIED", 403)
        if request.mode == "run":
            if not asset.principal:
                raise RegistryError("EXECUTION_PRINCIPAL_REQUIRED")
            resolved = self.registry.resolve(actor, asset.principal, version=request.version)
            if resolved.version.manifest.spec_digest != request.spec_digest:
                raise RegistryError("EXECUTION_VERSION_MISMATCH")
        # Evaluation does not bypass production admission: it cannot select a
        # production Version and is confined to explicitly allowed test resources.
        return asset, policy

    def _load(self, con, actor, grant_id, lock=False):
        query = select(grants).where(grants.c.id == grant_id, grants.c.zone == actor.zone)
        if lock:
            query = query.with_for_update()
        row = con.execute(query).mappings().first()
        if row is None:
            raise RegistryError("EXECUTION_GRANT_NOT_FOUND", 404)
        return ExecutionGrant.model_validate_json(row["document"])

    def _save(self, con, grant):
        con.execute(
            update(grants).where(grants.c.id == grant.id).values(document=grant.model_dump_json())
        )

    def get(self, actor, grant_id):
        self._policy(actor, "gateways")
        with self.engine.connect() as con:
            return self._load(con, actor, grant_id)

    def _reserve(self, actor, request):
        asset, _ = self._admit(actor, request)
        fingerprint = digest(canonical_bytes(request.model_dump()))
        with self.registry.inventory._transaction() as con:
            existing = (
                con.execute(
                    select(grants).where(
                        grants.c.zone == actor.zone,
                        grants.c.issuer == actor.subject,
                        grants.c.request_id == request.request_id,
                    )
                )
                .mappings()
                .first()
            )
            if existing:
                if existing["request_digest"] != fingerprint:
                    raise RegistryError("EXECUTION_REQUEST_CONFLICT")
                return ExecutionGrant.model_validate_json(existing["document"]), False
            now = self.registry.clock()
            grant = ExecutionGrant(
                id="grant-" + uuid4().hex,
                zone=actor.zone,
                issuer=actor.subject,
                request=request,
                worker_id="execution-" + uuid4().hex,
                owner_user_id=asset.metadata.owner_user_id,
                department_id=asset.metadata.department_id,
                status="issuing",
                cleanup_pending=True,
                created_at=now,
                expires_at=now + request.ttl_seconds * 1000,
            )
            con.execute(
                insert(grants).values(
                    id=grant.id,
                    zone=actor.zone,
                    issuer=actor.subject,
                    asset_id=request.asset_id,
                    request_id=request.request_id,
                    request_digest=fingerprint,
                    document=grant.model_dump_json(),
                )
            )
            return grant, True

    def _delegate(self, actor, grant):
        _, policy = self._admit(actor, grant.request)
        remaining_ms = grant.expires_at - self.registry.clock()
        if remaining_ms <= 0:
            raise RegistryError("EXECUTION_EXPIRED", 401)
        ttl = (remaining_ms + 999) // 1000
        # Empty VFS namespace. Pairwise operation/resource scopes are enforced
        # by authorize(), not by treating DelegationScope's sets as a matrix.
        return self.delegation.delegate(
            coordinator_agent_id=policy.coordinator_id,
            coordinator_owner_id=policy.coordinator_owner_id,
            worker_id=grant.worker_id,
            worker_name=grant.id,
            delegation_mode=DelegationMode.CLEAN,
            zone_id=actor.zone,
            ttl_seconds=min(300, ttl),
            can_sub_delegate=False,
            intent=f"product-agent:{grant.id}:{grant.request.mode}",
            scope=DelegationScope(
                allowed_operations=frozenset(p.operation for p in grant.request.permissions),
                resource_patterns=frozenset(p.resource for p in grant.request.permissions),
                max_depth=0,
            ),
        )

    def _activate(self, actor, grant_id, result, key_id):
        with self.registry.inventory._transaction() as con:
            grant = self._load(con, actor, grant_id, lock=True)
            if grant.status != "issuing" or self.registry.clock() >= grant.expires_at:
                raise RegistryError("EXECUTION_ISSUANCE_ABORTED")
            grant = grant.model_copy(
                update={
                    "delegation_id": result.delegation_id,
                    "key_id": key_id,
                    "status": "active",
                    "cleanup_pending": False,
                }
            )
            self._save(con, grant)
            return grant

    async def issue(self, actor: RegistryActor, request: IssueExecutionGrant):
        try:
            grant, created = await asyncio.to_thread(self._reserve, actor, request)
        except IntegrityError:
            # The winning request owns issuance; never create a second worker.
            grant, created = await asyncio.to_thread(self._reserve, actor, request)
        if not created:
            return ExecutionGrantResult(grant=grant)
        try:
            result = await asyncio.to_thread(self._delegate, actor, grant)
            auth = await self.auth.authenticate(result.api_key)
            if (
                not auth.authenticated
                or auth.subject_type != "agent"
                or auth.subject_id != grant.worker_id
                or auth.zone_id != actor.zone
                or auth.is_admin
                or auth.zone_perms != ((actor.zone, "rw"),)
            ):
                raise RegistryError("EXECUTION_IAM_IDENTITY_INVALID", 503)
            await asyncio.to_thread(self._admit, actor, request)
            current = await asyncio.to_thread(
                self._activate, actor, grant.id, result, auth.metadata["key_id"]
            )
            return ExecutionGrantResult(grant=current, api_key=result.api_key)
        except Exception:
            # Persist local denial first. A process crash leaves issuing (also
            # denied); operators can retry revoke after restart by grant ID.
            await asyncio.to_thread(self._stop, actor, grant.id, "failed", "issuance_failed")
            await asyncio.to_thread(self._cleanup, actor, grant.id)
            raise RegistryError("EXECUTION_ISSUANCE_FAILED", 503) from None

    def _stop(self, actor, grant_id, status, reason):
        with self.registry.inventory._transaction() as con:
            grant = self._load(con, actor, grant_id, lock=True)
            grant = grant.model_copy(
                update={
                    "status": "revoked" if grant.status == "revoked" else status,
                    "cleanup_pending": True,
                    "reason": reason,
                    "stopped_by": grant.stopped_by or actor.subject,
                    "stopped_at": grant.stopped_at or self.registry.clock(),
                }
            )
            self._save(con, grant)
            return grant

    def _cleanup(self, actor, grant_id):
        with self.engine.connect() as con:
            grant = self._load(con, actor, grant_id)
        if grant.status not in ("revoked", "failed") or not grant.cleanup_pending:
            return grant
        # Deterministic worker identity recovers a crash between IAM persistence
        # and binding activation. Store its delegation ID BEFORE cleanup.
        delegation = (
            self.delegation.get_delegation_by_id(grant.delegation_id)
            if grant.delegation_id
            else self.delegation.get_delegation(grant.worker_id)
        )
        if delegation:
            if delegation.agent_id != grant.worker_id or delegation.zone_id != actor.zone:
                raise RegistryError("EXECUTION_IAM_IDENTITY_INVALID", 503)
            with self.registry.inventory._transaction() as con:
                current = self._load(con, actor, grant_id, lock=True)
                self._save(
                    con, current.model_copy(update={"delegation_id": delegation.delegation_id})
                )
            try:
                if delegation.status == DelegationStatus.ACTIVE:
                    self.delegation.revoke_delegation(delegation.delegation_id)
                else:
                    self.delegation.cleanup_delegation(delegation.delegation_id)
            except Exception:
                # No secret/backend exception text in API or stored reason.
                return self._stop(actor, grant_id, grant.status, "iam_cleanup_pending")
        elif self.registry.clock() < grant.expires_at:
            # Another issuer may still be in flight. Never report cleanup done.
            return grant
        with self.registry.inventory._transaction() as con:
            current = self._load(con, actor, grant_id, lock=True)
            current = current.model_copy(update={"cleanup_pending": False})
            self._save(con, current)
            return current

    def revoke(self, actor, grant_id):
        self._policy(actor, "gateways")
        self._stop(actor, grant_id, "revoked", "explicit_revoke")
        return self._cleanup(actor, grant_id)

    def list(self, actor, asset_id=None, after=""):
        self._policy(actor, "gateways")
        query = select(grants.c.document).where(grants.c.zone == actor.zone, grants.c.id > after)
        if asset_id:
            query = query.where(grants.c.asset_id == asset_id)
        with self.engine.connect() as con:
            rows = con.execute(query.order_by(grants.c.id).limit(101)).scalars().all()
        items = [ExecutionGrant.model_validate_json(row) for row in rows[:100]]
        return ExecutionGrantPage(
            items=items, next_cursor=items[-1].id if len(rows) > 100 else None
        )

    async def authorize(self, actor: RegistryActor, check: CheckExecutionGrant):
        grant = await asyncio.to_thread(self.get, actor, check.grant_id)
        if grant.status != "active" or self.registry.clock() >= grant.expires_at:
            raise RegistryError("EXECUTION_INACTIVE", 401)
        if (
            check.task_id != grant.request.task_id
            or check.spec_digest != grant.request.spec_digest
            or (check.permission.operation, check.permission.resource)
            not in permissions(grant.request.permissions)
        ):
            raise RegistryError("EXECUTION_SCOPE_DENIED", 403)
        auth = await self.auth.authenticate(check.token.get_secret_value())
        if (
            not auth.authenticated
            or auth.subject_type != "agent"
            or auth.is_admin
            or auth.subject_id != grant.worker_id
            or auth.zone_id != actor.zone
            or auth.metadata.get("key_id") != grant.key_id
            or auth.zone_perms != ((actor.zone, "rw"),)
        ):
            raise RegistryError("EXECUTION_TOKEN_INVALID", 401)
        delegated = await asyncio.to_thread(
            self.delegation.get_delegation_by_id, grant.delegation_id
        )
        expiry = delegated.lease_expires_at if delegated else None
        if expiry and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if (
            not delegated
            or delegated.status != DelegationStatus.ACTIVE
            or not expiry
            or int(expiry.timestamp() * 1000) <= self.registry.clock()
            or delegated.zone_id != actor.zone
            or delegated.agent_id != grant.worker_id
            or delegated.can_sub_delegate
            or delegated.delegation_mode != DelegationMode.CLEAN
            or delegated.scope is None
            or delegated.scope.max_depth != 0
            or delegated.scope.allowed_operations
            != frozenset(p.operation for p in grant.request.permissions)
            or delegated.scope.resource_patterns
            != frozenset(p.resource for p in grant.request.permissions)
        ):
            raise RegistryError("EXECUTION_DELEGATION_INACTIVE", 401)
        issuer = RegistryActor(subject=grant.issuer, zone=actor.zone, is_admin=True)
        await asyncio.to_thread(self._admit, issuer, grant.request)
        # Catch revocation while admission/IAM checks were in progress.
        latest = await asyncio.to_thread(self.get, actor, grant.id)
        if latest.status != "active" or self.registry.clock() >= latest.expires_at:
            raise RegistryError("EXECUTION_INACTIVE", 401)
        return ExecutionAuthorization(grant=latest, authorized_at=self.registry.clock())
