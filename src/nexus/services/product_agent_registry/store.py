"""Primary-store publication authority. All writers serialize on their principal row."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    Text,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from nexus.contracts.product_agent_registry import (
    Channel,
    CommandResult,
    DecideVersion,
    Decision,
    MoveChannel,
    Principal,
    PrincipalRef,
    PublishVersion,
    RegisterPrincipal,
    RegistryCommand,
    Resolution,
    SetPrincipalStatus,
    SignedVersion,
    VersionItem,
    VersionManifest,
    VersionsPage,
    canonical_bytes,
)
from nexus.contracts.resource_ref import validate_resource_ref
from nexus.services.product_agent_registry.signing import key_id, verify_version

metadata = MetaData()
principals = Table(
    "product_agent_principals",
    metadata,
    Column("domain", String(253), primary_key=True),
    Column("name", String(128), primary_key=True),
    Column("owner_zone", String(255), nullable=False),
    Column("display_name", Text, nullable=False),
    Column("status", String(16), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)


def _child(name: str, *columns: Column) -> Table:
    return Table(
        name,
        metadata,
        Column("domain", String(253), primary_key=True),
        Column("name", String(128), primary_key=True),
        *columns,
        ForeignKeyConstraint(
            ["domain", "name"], ["product_agent_principals.domain", "product_agent_principals.name"]
        ),
    )


versions = _child(
    "product_agent_versions",
    Column("version", String(128), primary_key=True),
    Column("content_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
)
decisions = _child(
    "product_agent_decisions",
    Column("sequence", BigInteger, primary_key=True),
    Column("version", String(128), nullable=False),
    Column("document", Text, nullable=False),
)
channels = _child(
    "product_agent_channels",
    Column("channel", String(16), primary_key=True),
    Column("document", Text, nullable=False),
)
events = _child(
    "product_agent_events",
    Column("sequence", BigInteger, primary_key=True),
    Column("document", Text, nullable=False),
)
requests = _child(
    "product_agent_requests",
    Column("request_id", String(128), primary_key=True),
    Column("request_digest", String(71), nullable=False),
    Column("result", Text, nullable=False),
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class RegistryError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class RegistryActor:
    subject: str
    zone: str
    is_admin: bool


def initialize_schema(engine: Engine) -> None:
    """Explicit operator operation. Creates only new Registry tables; never touches legacy data."""
    metadata.create_all(engine)
    from nexus.services.product_agent_registry.inventory import initialize_inventory_schema

    initialize_inventory_schema(engine)
    from nexus.services.product_agent_registry.execution import initialize_execution_schema

    initialize_execution_schema(engine)


class ProductAgentRegistry:
    def __init__(
        self,
        engine: Engine,
        key: Ed25519PrivateKey,
        domain_zones: dict[str, str],
        clock=None,
        trusted_keys: dict[str, Ed25519PublicKey] | None = None,
        inventory_zone_domains: dict[str, str] | None = None,
        inventory_attestors: set[str] | None = None,
        inventory_enforced_zones: set[str] | None = None,
    ):
        if (
            not isinstance(domain_zones, dict)
            or not domain_zones
            or not isinstance(key, Ed25519PrivateKey)
        ):
            raise ValueError("REGISTRY_CONFIGURATION_REQUIRED")
        if engine.dialect.name not in ("sqlite", "postgresql"):
            raise ValueError("REGISTRY_DATABASE_UNSUPPORTED")
        for domain, zone in domain_zones.items():
            PrincipalRef(trust_domain=domain, agent_name="validation")
            if validate_resource_ref(
                {
                    "api_version": "common.sudo.dev/v1",
                    "kind": "ResourceRef",
                    "zone_id": zone,
                    "path": "/",
                }
            ):
                raise ValueError("REGISTRY_ZONE_INVALID")
        if not set(metadata.tables).issubset(inspect(engine).get_table_names()):
            raise RegistryError("REGISTRY_SCHEMA_REQUIRED", 503)
        self.engine, self.key, self.domain_zones = engine, key, dict(domain_zones)
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.key_id = key_id(key.public_key())
        self.trusted_keys = dict(trusted_keys or {})
        self.trusted_keys[self.key_id] = key.public_key()
        if any(
            not isinstance(value, Ed25519PublicKey) or name != key_id(value)
            for name, value in self.trusted_keys.items()
        ):
            raise ValueError("REGISTRY_TRUST_CONFIGURATION_INVALID")

        from nexus.services.product_agent_registry.inventory import AgentInventory

        self.inventory = AgentInventory(
            self, inventory_zone_domains, inventory_attestors, inventory_enforced_zones
        )

    def _authorize(self, actor: RegistryActor, ref: PrincipalRef, write=False) -> None:
        if not actor.subject or not actor.zone:
            raise RegistryError("REGISTRY_IDENTITY_REQUIRED", 401)
        if self.domain_zones.get(ref.trust_domain) != actor.zone or (write and not actor.is_admin):
            raise RegistryError("REGISTRY_FORBIDDEN", 403)

    @staticmethod
    def _scope(table: Table, ref: PrincipalRef):
        return (table.c.domain == ref.trust_domain) & (table.c.name == ref.agent_name)

    def _principal(
        self, con: Connection, ref: PrincipalRef, actor: RegistryActor, *, lock=False
    ) -> dict:
        query = select(principals).where(self._scope(principals, ref))
        if lock:
            query = query.with_for_update(read=True)
        row = con.execute(query).mappings().first()
        if row is None or row["owner_zone"] != actor.zone:
            raise RegistryError("AGENT_PRINCIPAL_NOT_FOUND", 404)
        return dict(row)

    @staticmethod
    def _view(row: dict) -> Principal:
        return Principal(
            principal=PrincipalRef(trust_domain=row["domain"], agent_name=row["name"]),
            owner_zone=row["owner_zone"],
            display_name=row["display_name"],
            status=row["status"],
            revision=row["revision"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _active(row: dict) -> None:
        if row["status"] != "active":
            raise RegistryError("AGENT_PRINCIPAL_" + row["status"].upper())

    def _version(self, con: Connection, ref: PrincipalRef, version: str) -> SignedVersion:
        doc = con.execute(
            select(versions.c.document).where(
                self._scope(versions, ref), versions.c.version == version
            )
        ).scalar_one_or_none()
        if doc is None:
            raise RegistryError("AGENT_VERSION_NOT_FOUND", 404)
        signed = SignedVersion.model_validate_json(doc)
        try:
            verify_version(signed, self.trusted_keys)
            if signed.manifest.principal != ref or signed.manifest.version != version:
                raise ValueError("registration identity mismatch")
            if signed.manifest.spec_ref.get("digest") != signed.manifest.spec_digest:
                raise ValueError("registration reference mismatch")
            if any(
                item["zone_id"] != self.domain_zones[ref.trust_domain]
                for item in [signed.manifest.spec_ref, *signed.manifest.source_evidence_refs]
            ):
                raise ValueError("registration zone mismatch")
        except ValueError:
            raise RegistryError("REGISTRY_SIGNATURE_INVALID", 503) from None
        return signed

    def _decision(self, con: Connection, ref: PrincipalRef, version: str) -> Decision | None:
        doc = con.execute(
            select(decisions.c.document)
            .where(self._scope(decisions, ref), decisions.c.version == version)
            .order_by(decisions.c.sequence.desc())
            .limit(1)
        ).scalar_one_or_none()
        return Decision.model_validate_json(doc) if doc else None

    def _approved(self, con: Connection, ref: PrincipalRef, version: str) -> Decision:
        decision = self._decision(con, ref, version)
        if decision and decision.decision == "revoked":
            raise RegistryError("AGENT_VERSION_REVOKED")
        if not decision or decision.decision != "approved":
            raise RegistryError("AGENT_VERSION_NOT_APPROVED")
        if decision.expires_at is None or decision.expires_at <= self.clock():
            raise RegistryError("AGENT_APPROVAL_EXPIRED")
        return decision

    def command(self, actor: RegistryActor, command: RegistryCommand) -> CommandResult:
        ref = command.principal
        self._authorize(actor, ref, write=True)
        key = {"domain": ref.trust_domain, "name": ref.agent_name}
        request_digest = digest(
            canonical_bytes({"subject": actor.subject, "command": command.model_dump()})
        )
        with self.engine.begin() as con:
            if con.dialect.name == "sqlite":
                con.exec_driver_sql("BEGIN IMMEDIATE")
            created = False
            if isinstance(command, RegisterPrincipal):
                try:
                    with con.begin_nested():
                        con.execute(
                            insert(principals).values(
                                **key,
                                owner_zone=actor.zone,
                                display_name=command.display_name,
                                status="active",
                                revision=0,
                                created_at=self.clock(),
                            )
                        )
                    created = True
                except IntegrityError:
                    pass  # Duplicate registration is checked against the original request below.
            # Acquires a row write lock on PostgreSQL and the writer lock on SQLite.
            result = con.execute(
                update(principals)
                .where(self._scope(principals, ref), principals.c.owner_zone == actor.zone)
                .values(revision=principals.c.revision)
            )
            if result.rowcount != 1:
                raise RegistryError("AGENT_PRINCIPAL_NOT_FOUND", 404)
            row = self._principal(con, ref, actor)
            prior = (
                con.execute(
                    select(requests).where(
                        self._scope(requests, ref), requests.c.request_id == command.request_id
                    )
                )
                .mappings()
                .first()
            )
            if prior:
                if prior["request_digest"] != request_digest:
                    raise RegistryError("REGISTRY_IDEMPOTENCY_CONFLICT")
                return CommandResult.model_validate_json(prior["result"])
            if isinstance(command, RegisterPrincipal) and not created:
                raise RegistryError("AGENT_PRINCIPAL_EXISTS")
            response = CommandResult(principal=self._view(row))
            if not isinstance(command, SetPrincipalStatus):
                self._active(row)
            sequence = row["revision"] + 1
            if isinstance(command, PublishVersion):
                self.inventory.admission(actor, ref, command.spec_digest, con)
                response.version = self._publish(con, key, actor, command)
            elif isinstance(command, DecideVersion):
                self._version(con, ref, command.version)
                previous = self._decision(con, ref, command.version)
                if previous and previous.decision == "revoked":
                    raise RegistryError("AGENT_VERSION_REVOKED")
                if command.decision == "approved":
                    self.inventory.admission(
                        actor,
                        ref,
                        self._version(con, ref, command.version).manifest.spec_digest,
                        con,
                    )
                    if (
                        command.expires_at is None
                        or not self.clock() < command.expires_at <= self.clock() + 7 * 86400000
                    ):
                        raise RegistryError("INVALID_APPROVAL_EXPIRY", 400)
                elif command.expires_at is not None:
                    raise RegistryError("INVALID_APPROVAL_EXPIRY", 400)
                decision = Decision(
                    id=str(uuid4()),
                    version=command.version,
                    decision=command.decision,
                    actor=actor.subject,
                    reason=command.reason,
                    expires_at=command.expires_at,
                    created_at=self.clock(),
                    registry_revision=sequence,
                )
                con.execute(
                    insert(decisions).values(
                        **key,
                        sequence=sequence,
                        version=command.version,
                        document=decision.model_dump_json(),
                    )
                )
                response.decision = decision
                if command.decision != "approved":
                    self._disable_channels(con, ref, command.version)
            elif isinstance(command, MoveChannel):
                signed = self._version(con, ref, command.version)
                self._approved(con, ref, command.version)
                self.inventory.admission(actor, ref, signed.manifest.spec_digest, con)
                prior_channel = self._channel(con, ref, command.channel)
                if command.expected_revision != (prior_channel.revision if prior_channel else 0):
                    raise RegistryError("CHANNEL_REVISION_CONFLICT")
                channel = Channel(
                    name=command.channel,
                    version=command.version,
                    spec_digest=signed.manifest.spec_digest,
                    revision=command.expected_revision + 1,
                    enabled=True,
                    updated_at=self.clock(),
                )
                if prior_channel:
                    con.execute(
                        update(channels)
                        .where(self._scope(channels, ref), channels.c.channel == command.channel)
                        .values(document=channel.model_dump_json())
                    )
                else:
                    con.execute(
                        insert(channels).values(
                            **key, channel=command.channel, document=channel.model_dump_json()
                        )
                    )
                response.channel = channel
            elif isinstance(command, SetPrincipalStatus):
                if row["revision"] != command.expected_revision:
                    raise RegistryError("PRINCIPAL_REVISION_CONFLICT")
                if row["status"] == "revoked":
                    raise RegistryError("AGENT_PRINCIPAL_REVOKED")
                row["status"] = command.status
                if command.status != "active":
                    self._disable_channels(con, ref)
            row["revision"] = sequence
            con.execute(
                update(principals)
                .where(self._scope(principals, ref))
                .values(revision=sequence, status=row["status"])
            )
            response.principal = self._view(row)
            con.execute(
                insert(events).values(
                    **key,
                    sequence=sequence,
                    document=json.dumps(
                        {
                            "kind": command.kind,
                            "request_id": command.request_id,
                            "actor": actor.subject,
                            "created_at": self.clock(),
                            "reason": getattr(command, "reason", None),
                            "version": getattr(command, "version", None),
                            "channel": response.channel.model_dump() if response.channel else None,
                            "status": row["status"],
                        }
                    ),
                )
            )
            con.execute(
                insert(requests).values(
                    **key,
                    request_id=command.request_id,
                    request_digest=request_digest,
                    result=response.model_dump_json(),
                )
            )
            return response

    def _publish(self, con, key, actor, command: PublishVersion) -> SignedVersion:
        if command.version == "latest" or command.spec_ref.get("digest") != command.spec_digest:
            raise RegistryError("IMMUTABLE_SPEC_REF_REQUIRED", 400)
        for ref in [command.spec_ref, *command.source_evidence_refs]:
            if ref["zone_id"] != actor.zone:
                raise RegistryError("REGISTRY_RESOURCE_ZONE_FORBIDDEN", 403)
            if not ref.get("digest"):
                raise RegistryError("IMMUTABLE_SPEC_REF_REQUIRED", 400)
        content = {
            k: v
            for k, v in command.model_dump().items()
            if k not in ["request_id", "kind", "api_version"]
        }
        content_digest = digest(canonical_bytes(content))
        prior = (
            con.execute(
                select(versions).where(
                    self._scope(versions, command.principal), versions.c.version == command.version
                )
            )
            .mappings()
            .first()
        )
        if prior:
            if prior["content_digest"] != content_digest:
                raise RegistryError("IMMUTABLE_VERSION_CONFLICT")
            return self._version(con, command.principal, command.version)
        manifest = VersionManifest(**content, created_at=self.clock(), created_by=actor.subject)
        payload = canonical_bytes(manifest.model_dump())
        signed = SignedVersion(
            manifest=manifest,
            payload_base64=base64.b64encode(payload).decode(),
            digest=digest(payload),
            key_id=self.key_id,
            signature_base64=base64.b64encode(self.key.sign(payload)).decode(),
        )
        con.execute(
            insert(versions).values(
                **key,
                version=command.version,
                content_digest=content_digest,
                document=signed.model_dump_json(),
            )
        )
        return signed

    def _channel(self, con, ref, name) -> Channel | None:
        doc = con.execute(
            select(channels.c.document).where(
                self._scope(channels, ref), channels.c.channel == name
            )
        ).scalar_one_or_none()
        return Channel.model_validate_json(doc) if doc else None

    def _disable_channels(self, con, ref, version=None):
        for doc in con.execute(
            select(channels.c.document).where(self._scope(channels, ref))
        ).scalars():
            channel = Channel.model_validate_json(doc)
            if channel.enabled and (version is None or channel.version == version):
                channel.enabled = False
                channel.revision += 1
                channel.updated_at = self.clock()
                con.execute(
                    update(channels)
                    .where(self._scope(channels, ref), channels.c.channel == channel.name)
                    .values(document=channel.model_dump_json())
                )

    @contextmanager
    def _read(self, actor, ref):
        self._authorize(actor, ref)
        with self.engine.begin() as con:
            if con.dialect.name == "sqlite":
                con.exec_driver_sql("BEGIN")
            # SELECT FOR SHARE is read-only and serializes with all Registry writers.
            row = self._principal(con, ref, actor, lock=True)
            yield con, row

    def resolve(
        self,
        actor: RegistryActor,
        ref: PrincipalRef,
        version: str | None = None,
        channel: str | None = None,
    ) -> Resolution:
        if version is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
            raise RegistryError("INVALID_AGENT_SELECTOR", 400)
        if channel is not None and channel not in ("stable", "canary"):
            raise RegistryError("INVALID_AGENT_SELECTOR", 400)
        if version is not None and channel is not None:
            raise RegistryError("AMBIGUOUS_AGENT_SELECTOR", 400)
        with self._read(actor, ref) as (con, row):
            self._active(row)
            selected = None
            if not version:
                if (channel or "stable") not in ["stable", "canary"]:
                    raise RegistryError("AGENT_CHANNEL_UNAVAILABLE", 404)
                selected = self._channel(con, ref, channel or "stable")
                if selected is None or not selected.enabled:
                    raise RegistryError("AGENT_CHANNEL_UNAVAILABLE", 404)
                version = selected.version
            signed = self._version(con, ref, version)
            approval = self._approved(con, ref, version)
            self.inventory.admission(actor, ref, signed.manifest.spec_digest, con)
            return Resolution(
                principal=self._view(row),
                version=signed,
                approval=approval,
                channel=selected,
                evaluated_at=self.clock(),
            )

    def version(self, actor: RegistryActor, ref: PrincipalRef, version: str) -> VersionItem:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version):
            raise RegistryError("INVALID_AGENT_SELECTOR", 400)
        with self._read(actor, ref) as (con, _row):
            return VersionItem(
                version=self._version(con, ref, version), decision=self._decision(con, ref, version)
            )

    def list_versions(
        self, actor: RegistryActor, ref: PrincipalRef, after: str = ""
    ) -> VersionsPage:
        if after and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", after):
            raise RegistryError("INVALID_AGENT_CURSOR", 400)
        with self._read(actor, ref) as (con, row):
            labels = list(
                con.execute(
                    select(versions.c.version)
                    .where(self._scope(versions, ref), versions.c.version > after)
                    .order_by(versions.c.version)
                    .limit(51)
                ).scalars()
            )
            items = [
                {
                    "version": self._version(con, ref, label),
                    "decision": self._decision(con, ref, label),
                }
                for label in labels[:50]
            ]
            current_channels = [
                Channel.model_validate_json(doc)
                for doc in con.execute(
                    select(channels.c.document)
                    .where(self._scope(channels, ref))
                    .order_by(channels.c.channel)
                ).scalars()
            ]
            return VersionsPage(
                principal=self._view(row),
                items=items,
                channels=current_channels,
                next_cursor=labels[49] if len(labels) > 50 else None,
            )
