"""Zone-scoped product inventory; gateway assertions never become execution grants."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager

from sqlalchemy import (
    BigInteger,
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

from nexus.contracts.agent_inventory import (
    AcceptanceReport,
    AssetCommand,
    AssetEvent,
    AssetHistory,
    AssetMetadata,
    AssetPage,
    AssetRecord,
    LinkAsset,
    MergeAsset,
    ObserveAsset,
    RecordAcceptance,
    ReviewAsset,
    SaveAsset,
    SubmitAsset,
    SuspendAsset,
)
from nexus.contracts.product_agent_registry import PrincipalRef, canonical_bytes
from nexus.services.product_agent_registry.store import RegistryActor, RegistryError

metadata = MetaData()
assets = Table(
    "product_agent_assets",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("source_key", String(71), nullable=False),
    Column("owner_id", String(150), nullable=False),
    Column("revision", BigInteger, nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "source_key", name="uq_agent_asset_source"),
)
links = Table(
    "product_agent_asset_links",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("domain", String(253), primary_key=True),
    Column("name", String(128), primary_key=True),
    Column("asset_id", String(150), nullable=False),
    UniqueConstraint("zone", "asset_id", name="uq_agent_asset_principal"),
)
requests = Table(
    "product_agent_asset_requests",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("request_id", String(128), primary_key=True),
    Column("digest", String(71), nullable=False),
    Column("result", Text, nullable=False),
)
acceptances = Table(
    "product_agent_asset_acceptances",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("asset_id", String(150), primary_key=True),
    Column("spec_digest", String(71), primary_key=True),
    Column("metadata_digest", String(71), nullable=False),
    Column("approved", BigInteger, nullable=False),
    Column("issuer", Text, nullable=False),
    Column("document", Text, nullable=False),
)
events = Table(
    "product_agent_asset_events",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("asset_id", String(150), primary_key=True),
    Column("revision", BigInteger, primary_key=True),
    Column("document", Text, nullable=False),
)


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def source_key(value: AssetMetadata) -> str:
    return digest([value.source_platform, value.source_instance, value.source_agent_id])


def initialize_inventory_schema(engine):
    """Explicit additive schema creation; no conversion, deletion or writer fencing."""
    metadata.create_all(engine)


class AgentInventory:
    def __init__(self, registry, zone_domains=None, attestors=None, enforced_zones=None):
        self.registry, self.engine = registry, registry.engine
        self.zone_domains = dict(zone_domains or {})
        self.attestors = set(attestors or [])
        self.enforced_zones = set(enforced_zones or []) | set(self.zone_domains)
        zones = set(registry.domain_zones.values())
        if any(
            z not in zones or v not in ("office", "core") for z, v in self.zone_domains.items()
        ) or not self.enforced_zones.issubset(zones):
            raise ValueError("REGISTRY_INVENTORY_CONFIGURATION_INVALID")
        if self.enforced_zones:
            self._ready()

    def _ready(self):
        if not set(metadata.tables).issubset(inspect(self.engine).get_table_names()):
            raise RegistryError("REGISTRY_INVENTORY_SCHEMA_REQUIRED", 503)

    def _authorize(self, actor: RegistryActor):
        self._ready()
        # Only the authenticated management gateway/IT role writes or reads full assets.
        # Moss separately enforces end-user ownership and review permissions.
        if (
            not actor.subject
            or not actor.is_admin
            or actor.zone not in self.registry.domain_zones.values()
        ):
            raise RegistryError("REGISTRY_INVENTORY_FORBIDDEN", 403)

    @staticmethod
    def _where(zone, asset_id):
        return (assets.c.zone == zone) & (assets.c.id == asset_id)

    def _get(self, con, zone, asset_id, lock=False, read=False):
        query = select(assets.c.document).where(self._where(zone, asset_id))
        if lock:
            query = query.with_for_update(read=read)
        value = con.execute(query).scalar_one_or_none()
        if value is None:
            raise RegistryError("AGENT_ASSET_NOT_FOUND", 404)
        return AssetRecord.model_validate_json(value)

    def _recommend(self, zone, values):
        reasons = []
        if values.hosting == "public_saas":
            return "L0", ["public_saas_outside_execution_control"], "external"
        if (
            self.zone_domains.get(zone) == "core"
            or values.domain == "core"
            or any(c.classification == "core" for c in values.data_connections)
        ):
            reasons.append("core_domain_or_core_data_requires_L2")
        if any(
            c.classification == "confidential" and set(c.actions) & {"write", "delete", "export"}
            for c in values.data_connections
        ):
            reasons.append("sensitive_data_mutation_requires_L2")
        return (
            ("L2", reasons, "high")
            if reasons
            else (
                "L1",
                ["office_gateway_control"],
                "medium" if values.data_connections or values.tool_refs else "low",
            )
        )

    def _new(self, zone, command):
        values = command.metadata
        recommended, reasons, risk = self._recommend(zone, values)
        return AssetRecord(
            id=command.asset_id,
            owner_zone=zone,
            metadata=values,
            metadata_digest=digest(values.model_dump()),
            revision=0,
            status="discovered" if isinstance(command, ObserveAsset) else "draft",
            actual_level="L0",
            recommended_level=recommended,
            recommendation_reasons=reasons,
            risk=risk,
            external_domain=values.hosting == "public_saas",
            observations=[],
            created_at=self.registry.clock(),
            updated_at=self.registry.clock(),
        )

    def _persist(self, con, actor, record, command):
        record.revision += 1
        record.updated_at = self.registry.clock()
        con.execute(
            update(assets)
            .where(self._where(actor.zone, record.id))
            .values(
                owner_id=record.metadata.owner_user_id,
                revision=record.revision,
                document=record.model_dump_json(),
            )
        )
        audit = AssetEvent(
            revision=record.revision,
            operation=command.kind,
            actor=actor.subject,
            initiated_by=command.initiated_by,
            created_at=record.updated_at,
            reason=getattr(command, "reason", None),
        )
        con.execute(
            insert(events).values(
                zone=actor.zone,
                asset_id=record.id,
                revision=record.revision,
                document=audit.model_dump_json(),
            )
        )

    def _projection(self, record):
        """Fresh evidence/risk presentation without changing the stored audit record."""
        record = record.model_copy(deep=True)
        record.recommended_level, record.recommendation_reasons, record.risk = self._recommend(
            record.owner_zone, record.metadata
        )
        if record.external_domain:
            record.risk = "external"
        record.evidence_status = "none"
        if record.acceptance:
            record.evidence_status = "current"
            if record.acceptance_issuer not in self.attestors:
                record.evidence_status = "untrusted"
            elif record.acceptance.expires_at <= self.registry.clock():
                record.evidence_status = "expired"
            elif record.status != "approved" or record.external_domain:
                record.evidence_status = "blocked"
        if record.evidence_status != "current":
            record.actual_level = "L0"
        return record

    @contextmanager
    def _transaction(self):
        with self.engine.begin() as con:
            if con.dialect.name == "sqlite":
                con.exec_driver_sql("BEGIN IMMEDIATE")
            yield con

    def command(self, actor: RegistryActor, command: AssetCommand) -> AssetRecord:
        self._authorize(actor)
        if isinstance(command, RecordAcceptance) and actor.subject not in self.attestors:
            raise RegistryError("AGENT_ACCEPTANCE_ISSUER_FORBIDDEN", 403)
        fingerprint = digest({"actor": actor.subject, "command": command.model_dump()})
        try:
            with self._transaction() as con:
                # Idempotency is scoped to the authenticated zone and exact actor+body.
                previous = (
                    con.execute(
                        select(requests).where(
                            requests.c.zone == actor.zone,
                            requests.c.request_id == command.request_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous:
                    if previous["digest"] != fingerprint:
                        raise RegistryError("REGISTRY_IDEMPOTENCY_CONFLICT")
                    return self._projection(AssetRecord.model_validate_json(previous["result"]))
                if (
                    isinstance(command, (SaveAsset, ObserveAsset))
                    and command.expected_revision == 0
                ):
                    record = self._new(actor.zone, command)
                    con.execute(
                        insert(assets).values(
                            zone=actor.zone,
                            id=record.id,
                            source_key=source_key(record.metadata),
                            owner_id=record.metadata.owner_user_id,
                            revision=0,
                            document=record.model_dump_json(),
                        )
                    )
                # Merge locks both rows in stable order on PostgreSQL.
                ids = (
                    sorted({command.asset_id, command.target_asset_id})
                    if isinstance(command, MergeAsset)
                    else [command.asset_id]
                )
                locked = {
                    asset_id: self._get(con, actor.zone, asset_id, lock=True) for asset_id in ids
                }
                record = locked[command.asset_id]
                # Another transaction may have committed the same request while we waited.
                previous = (
                    con.execute(
                        select(requests).where(
                            requests.c.zone == actor.zone,
                            requests.c.request_id == command.request_id,
                        )
                    )
                    .mappings()
                    .first()
                )
                if previous:
                    if previous["digest"] != fingerprint:
                        raise RegistryError("REGISTRY_IDEMPOTENCY_CONFLICT")
                    return self._projection(AssetRecord.model_validate_json(previous["result"]))
                if record.revision != command.expected_revision:
                    raise RegistryError("AGENT_ASSET_REVISION_CONFLICT")
                if record.status == "merged":
                    raise RegistryError("AGENT_ASSET_MERGED")
                if isinstance(command, SaveAsset):
                    if source_key(record.metadata) != source_key(command.metadata):
                        raise RegistryError("AGENT_ASSET_SOURCE_IMMUTABLE")
                    proposed_level, _, _ = self._recommend(actor.zone, command.metadata)
                    if (
                        record.revision
                        and record.recommended_level == "L2"
                        and proposed_level != "L2"
                    ):
                        raise RegistryError("AGENT_RISK_REDUCTION_REQUIRES_MIGRATION")
                    record.metadata = command.metadata
                    record.metadata_digest = digest(command.metadata.model_dump())
                    record.recommended_level, record.recommendation_reasons, record.risk = (
                        self._recommend(actor.zone, command.metadata)
                    )
                    record.external_domain = command.metadata.hosting == "public_saas" or any(
                        o.external_domain for o in record.observations
                    )
                    record.status, record.actual_level = "draft", "L0"
                    record.acceptance = record.acceptance_issuer = record.review_reason = None
                elif isinstance(command, ObserveAsset):
                    if source_key(record.metadata) != source_key(command.metadata):
                        raise RegistryError("AGENT_ASSET_SOURCE_IMMUTABLE")
                    if command.observation.observed_at > self.registry.clock() + 30000:
                        raise RegistryError("AGENT_DISCOVERY_TIME_INVALID", 400)
                    if command.observation not in record.observations:
                        if len(record.observations) >= 64:
                            raise RegistryError("AGENT_DISCOVERY_LIMIT", 400)
                        record.observations.append(command.observation)
                    record.external_domain = (
                        record.external_domain or command.observation.external_domain
                    )
                    if record.external_domain:
                        record.risk = "external"
                elif isinstance(command, SubmitAsset):
                    if record.status not in ("draft", "returned", "discovered", "suspended"):
                        raise RegistryError("AGENT_ASSET_STATE_CONFLICT")
                    record.status = "submitted"
                    record.review_reason = None
                elif isinstance(command, ReviewAsset):
                    if record.status != "submitted":
                        raise RegistryError("AGENT_ASSET_STATE_CONFLICT")
                    record.status = "approved" if command.decision == "approve" else "returned"
                    record.review_reason = command.reason
                    # Registration approval never certifies a claimed L1/L2/L3 level.
                    record.actual_level = "L0"
                    if command.decision == "approve" and record.acceptance:
                        if record.acceptance_issuer not in self.attestors:
                            raise RegistryError("AGENT_ACCEPTANCE_ISSUER_FORBIDDEN", 403)
                        self._validate_acceptance(record, record.acceptance)
                        record.actual_level = record.acceptance.level
                        con.execute(
                            update(acceptances)
                            .where(
                                acceptances.c.zone == actor.zone,
                                acceptances.c.asset_id == record.id,
                                acceptances.c.spec_digest == record.acceptance.spec_digest,
                            )
                            .values(approved=1)
                        )
                elif isinstance(command, SuspendAsset):
                    record.status = "suspended"
                    record.review_reason = command.reason
                elif isinstance(command, LinkAsset):
                    self.registry._authorize(actor, command.principal, write=True)
                    self.registry._principal(con, command.principal, actor)
                    if record.principal and record.principal != command.principal:
                        raise RegistryError("AGENT_ASSET_LINK_IMMUTABLE")
                    if not record.principal:
                        con.execute(
                            insert(links).values(
                                zone=actor.zone,
                                domain=command.principal.trust_domain,
                                name=command.principal.agent_name,
                                asset_id=record.id,
                            )
                        )
                    record.principal = command.principal
                elif isinstance(command, MergeAsset):
                    target = locked[command.target_asset_id]
                    source_level, _, _ = self._recommend(actor.zone, record.metadata)
                    target_level, _, _ = self._recommend(actor.zone, target.metadata)
                    if source_level == "L2" and target_level != "L2":
                        raise RegistryError("AGENT_RISK_REDUCTION_REQUIRES_MIGRATION")
                    if (
                        target.id == record.id
                        or target.revision != command.target_revision
                        or target.status == "merged"
                        or record.principal
                        or target.principal
                    ):
                        raise RegistryError("AGENT_ASSET_MERGE_CONFLICT")
                    # A merge is an explicit metadata decision, never a way to move live authority.
                    combined = {
                        digest(o.model_dump()): o
                        for o in [*target.observations, *record.observations]
                    }
                    if len(combined) > 64:
                        raise RegistryError("AGENT_DISCOVERY_LIMIT", 400)
                    target.observations = list(combined.values())
                    target.external_domain = target.external_domain or record.external_domain
                    target.status, target.actual_level = "draft", "L0"
                    target.acceptance = target.acceptance_issuer = None
                    if target.external_domain:
                        target.risk = "external"
                    record.status, record.merged_into = "merged", target.id
                    self._persist(con, actor, target, command)
                elif isinstance(command, RecordAcceptance):
                    self._validate_acceptance(record, command.report)
                    report_where = (
                        (acceptances.c.zone == actor.zone)
                        & (acceptances.c.asset_id == record.id)
                        & (acceptances.c.spec_digest == command.report.spec_digest)
                    )
                    existing = con.execute(
                        select(acceptances.c.document).where(report_where)
                    ).scalar_one_or_none()
                    values = {
                        "metadata_digest": record.metadata_digest,
                        "approved": 0,
                        "issuer": actor.subject,
                        "document": command.report.model_dump_json(),
                    }
                    if existing:
                        con.execute(update(acceptances).where(report_where).values(**values))
                    else:
                        con.execute(
                            insert(acceptances).values(
                                zone=actor.zone,
                                asset_id=record.id,
                                spec_digest=command.report.spec_digest,
                                **values,
                            )
                        )
                    record.acceptance, record.acceptance_issuer = command.report, actor.subject
                    # Acceptance still requires a fresh IT review before execution admission.
                    record.actual_level, record.status = "L0", "submitted"
                self._persist(con, actor, record, command)
                con.execute(
                    insert(requests).values(
                        zone=actor.zone,
                        request_id=command.request_id,
                        digest=fingerprint,
                        result=record.model_dump_json(),
                    )
                )
                return self._projection(record)
        except IntegrityError:
            # A concurrent identical create may have won; never hide a changed request.
            with self.engine.connect() as con:
                previous = (
                    con.execute(
                        select(requests).where(
                            requests.c.zone == actor.zone,
                            requests.c.request_id == command.request_id,
                        )
                    )
                    .mappings()
                    .first()
                )
            if previous and previous["digest"] == fingerprint:
                return self._projection(AssetRecord.model_validate_json(previous["result"]))
            raise RegistryError("AGENT_ASSET_IDENTITY_CONFLICT") from None

    def _validate_acceptance(self, record, report):
        required = {"entry", "identity", "router", "revoke"}
        if record.metadata.tool_refs:
            required.add("tool")
        if report.level in ("L2", "L3"):
            required.update({"isolation", "evaluation", "trace"})
            if record.metadata.hosting != "internal_hosted" or not all(
                [
                    report.execution_revision,
                    report.evaluation_id,
                    report.dataset_digest,
                    report.rubric_digest,
                ]
            ):
                raise RegistryError("AGENT_HOSTED_EVIDENCE_REQUIRED")
            if (
                any(
                    v is None
                    for v in [report.before_score, report.after_score, report.allowed_drop]
                )
                or report.after_score + report.allowed_drop < report.before_score
            ):
                raise RegistryError("AGENT_MIGRATION_EVALUATION_FAILED")
        if report.level == "L3":
            required.add("native")
        if (
            report.asset_id != record.id
            or report.metadata_digest != record.metadata_digest
            or (record.metadata.hosting == "public_saas" or record.external_domain)
            or not required.issubset(report.checks)
        ):
            raise RegistryError("AGENT_ACCEPTANCE_MISMATCH")
        if not self.registry.clock() < report.expires_at <= self.registry.clock() + 7 * 86400000:
            raise RegistryError("AGENT_ACCEPTANCE_EXPIRED")
        if int(report.level[1]) > int(record.metadata.requested_level[1]):
            raise RegistryError("AGENT_ACCEPTANCE_SCOPE_EXCEEDED")

    def get(self, actor, asset_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            return self._projection(self._get(con, actor.zone, asset_id))

    def list(self, actor, after="", owner=None):
        self._authorize(actor)
        if after and (len(after) > 150 or not all(c.isalnum() or c in "_.:@-" for c in after)):
            raise RegistryError("INVALID_ASSET_CURSOR", 400)
        query = select(assets.c.document).where(assets.c.zone == actor.zone, assets.c.id > after)
        if owner is not None:
            query = query.where(assets.c.owner_id == owner)
        with self.engine.connect() as con:
            rows = list(con.execute(query.order_by(assets.c.id).limit(101)).scalars())
        records = [self._projection(AssetRecord.model_validate_json(row)) for row in rows[:100]]
        return AssetPage(items=records, next_cursor=records[-1].id if len(rows) > 100 else None)

    def history(self, actor, asset_id, after=0):
        self._authorize(actor)
        if type(after) is not int or after < 0:
            raise RegistryError("INVALID_ASSET_CURSOR", 400)
        with self.engine.connect() as con:
            self._get(con, actor.zone, asset_id)
            rows = list(
                con.execute(
                    select(events.c.document)
                    .where(
                        events.c.zone == actor.zone,
                        events.c.asset_id == asset_id,
                        events.c.revision > after,
                    )
                    .order_by(events.c.revision)
                    .limit(101)
                ).scalars()
            )
        result = [AssetEvent.model_validate_json(row) for row in rows[:100]]
        return AssetHistory(
            asset_id=asset_id,
            events=result,
            next_revision=result[-1].revision if len(rows) > 100 else None,
        )

    def admission(self, actor, ref: PrincipalRef, spec_digest: str, con=None):
        """Read-only at publication, channel selection and every execution resolution."""
        self.registry._authorize(actor, ref)
        if not set(metadata.tables).issubset(inspect(self.engine).get_table_names()):
            if actor.zone not in self.enforced_zones:
                return None
            self._ready()

        def check(connection):
            asset_id = connection.execute(
                select(links.c.asset_id).where(
                    links.c.zone == actor.zone,
                    links.c.domain == ref.trust_domain,
                    links.c.name == ref.agent_name,
                )
            ).scalar_one_or_none()
            if not asset_id:
                if actor.zone not in self.enforced_zones:
                    return None
                raise RegistryError("AGENT_ASSET_REGISTRATION_REQUIRED")
            record = self._get(connection, actor.zone, asset_id, lock=True, read=True)
            if record.status != "approved" or record.actual_level == "L0" or not record.acceptance:
                raise RegistryError("AGENT_ASSET_NOT_ADMITTED")
            accepted = (
                connection.execute(
                    select(acceptances).where(
                        acceptances.c.zone == actor.zone,
                        acceptances.c.asset_id == asset_id,
                        acceptances.c.spec_digest == spec_digest,
                        acceptances.c.metadata_digest == record.metadata_digest,
                        acceptances.c.approved == 1,
                    )
                )
                .mappings()
                .first()
            )
            if not accepted or accepted["issuer"] not in self.attestors:
                raise RegistryError("AGENT_ACCEPTANCE_VERSION_MISMATCH")
            report = AcceptanceReport.model_validate_json(accepted["document"])
            self._validate_acceptance(record, report)
            recommended, _, _ = self._recommend(actor.zone, record.metadata)
            if recommended == "L2" and report.level not in ("L2", "L3"):
                raise RegistryError("AGENT_CORE_REQUIRES_L2")
            return record

        if con is not None:
            return check(con)
        with self.engine.connect() as connection:
            return check(connection)
