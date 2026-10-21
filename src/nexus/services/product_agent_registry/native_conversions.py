"""Immutable source-to-native mapping. Registration never changes source admission.

The target is a separate explicitly registered asset, so preparing a conversion
does not invalidate the source's L2 metadata or its currently published version.
L3 admission remains a separate guarded operation.
"""

import base64
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
)
from sqlalchemy.exc import IntegrityError

from nexus.contracts.agent_inventory import AcceptanceReport
from nexus.contracts.agent_native import (
    NativeConversion,
    NativeConversionPage,
    NativeDefinition,
    RegisterNativeConversion,
)
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.hosted_build import BASE
from nexus.services.product_agent_registry.inventory import acceptances
from nexus.services.product_agent_registry.store import RegistryError, digest

metadata = MetaData()
conversions = Table(
    "product_agent_native_conversions",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("asset_id", String(150), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "request_id"),
)


def initialize_native_schema(engine):
    """Explicit additive initialization; not invoked by reads or normal startup."""
    metadata.create_all(engine)


class NativeConversions:
    def __init__(self, registry):
        self.registry, self.engine = registry, registry.engine

    def _authorize(self, actor):
        self.registry.inventory._authorize(actor)
        if not set(metadata.tables).issubset(inspect(self.engine).get_table_names()):
            raise RegistryError("NATIVE_SCHEMA_REQUIRED", 503)

    @staticmethod
    def _decode(document):
        record = NativeConversion.model_validate_json(document)
        additions = {}
        for field, value, expected in (
            (
                "source_acceptance_payload_base64",
                record.source_acceptance,
                record.source_acceptance_digest,
            ),
            ("definition_payload_base64", record.definition, record.image.definition_digest),
        ):
            raw = canonical_bytes(value.model_dump())
            if digest(raw) != expected:
                raise RegistryError("NATIVE_STORED_MATERIAL_MISMATCH", 503)
            encoded = base64.b64encode(raw).decode()
            if getattr(record, field) not in (None, encoded):
                raise RegistryError("NATIVE_STORED_MATERIAL_MISMATCH", 503)
            additions[field] = encoded
        # Older records already bind both hashes. This is a read-only transport
        # projection of those exact bytes, never an implicit migration/write.
        return record.model_copy(update=additions)

    def register(self, actor, command: RegisterNativeConversion):
        self._authorize(actor)
        source = self.registry.hosted.get(actor, command.source_artifact_id)
        if source.dify_plan is None:
            raise RegistryError("NATIVE_SOURCE_PROFILE_UNSUPPORTED", 400)
        try:
            definition = NativeDefinition(plan=source.dify_plan)
        except ValueError:
            raise RegistryError("NATIVE_SOURCE_PROFILE_UNSUPPORTED", 400) from None
        image = command.image
        if (
            image.image_id == "sha256:" + "0" * 64
            or image.source_digest != source.image.source_digest
            or image.model_resource != source.image.model_resource
            or image.model_name != source.image.model_name
            or image.max_output_tokens != source.image.max_output_tokens
            or image.definition_digest != digest(canonical_bytes(definition.model_dump()))
            or image.dependency_lock_digest
            != digest(canonical_bytes({"runtime": image.runtime.model_dump(), "base_image": BASE}))
        ):
            raise RegistryError("NATIVE_MATERIAL_MISMATCH", 400)
        fingerprint = digest(
            canonical_bytes({"actor": actor.subject, "command": command.model_dump()})
        )

        def previous(con):
            return (
                con.execute(
                    select(conversions).where(
                        conversions.c.zone == actor.zone,
                        conversions.c.request_id == command.request_id,
                    )
                )
                .mappings()
                .first()
            )

        def replay(row):
            if row["request_digest"] != fingerprint:
                raise RegistryError("NATIVE_IDEMPOTENCY_CONFLICT")
            return self._decode(row["document"])

        try:
            with self.registry.inventory._transaction() as con:
                row = previous(con)
                if row:
                    return replay(row)
                # Stable ordering prevents opposing conversions deadlocking on PG.
                records = {
                    asset_id: self.registry.inventory._get(con, actor.zone, asset_id, lock=True)
                    for asset_id in sorted({source.asset_id, command.asset_id})
                }
                original, target = records[source.asset_id], records[command.asset_id]
                accepted = (
                    con.execute(
                        select(acceptances).where(
                            acceptances.c.zone == actor.zone,
                            acceptances.c.asset_id == source.asset_id,
                            acceptances.c.spec_digest == source.spec_digest,
                            acceptances.c.metadata_digest == source.metadata_digest,
                            acceptances.c.approved == 1,
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    original.status != "approved"
                    or original.metadata_digest != source.metadata_digest
                    or original.metadata.hosting != "internal_hosted"
                    or accepted is None
                    or accepted["issuer"] not in self.registry.inventory.attestors
                ):
                    raise RegistryError("NATIVE_REVIEWED_L2_SOURCE_REQUIRED")
                report = AcceptanceReport.model_validate_json(accepted["document"])
                if report.level != "L2":
                    raise RegistryError("NATIVE_REVIEWED_L2_SOURCE_REQUIRED")
                self.registry.inventory._validate_acceptance(
                    original, report, con=con, zone=actor.zone
                )
                if (
                    target.id == original.id
                    or target.status in ("suspended", "merged")
                    or target.metadata_digest != command.metadata_digest
                    or target.metadata.source_platform != "native"
                    or target.metadata.hosting != "internal_hosted"
                    or target.metadata.requested_level != "L3"
                    or not target.metadata.owner_user_id
                    or target.external_domain
                    or target.metadata.tool_refs
                    or target.metadata.data_connections
                    or target.metadata.model_paths != [image.model_resource]
                    or (
                        self.registry.inventory._projection(original).effective_domain == "core"
                        and self.registry.inventory._projection(target).effective_domain != "core"
                    )
                ):
                    raise RegistryError("NATIVE_TARGET_NOT_ADMITTED")
                provenance = {
                    "source_asset_id": source.asset_id,
                    "source_artifact_id": source.id,
                    "source_metadata_digest": source.metadata_digest,
                    "source_spec_digest": source.spec_digest,
                    "source_acceptance_digest": digest(canonical_bytes(report.model_dump())),
                }
                payload = canonical_bytes(
                    {
                        "kind": "NativeConversionSpec",
                        "asset_id": target.id,
                        "metadata_digest": target.metadata_digest,
                        **provenance,
                        "image": image.model_dump(),
                        "definition": definition.model_dump(),
                    }
                )
                record = NativeConversion(
                    id=str(uuid4()),
                    zone=actor.zone,
                    asset_id=target.id,
                    metadata_digest=target.metadata_digest,
                    **provenance,
                    source_acceptance=report,
                    source_acceptance_issuer=accepted["issuer"],
                    source_acceptance_payload_base64=base64.b64encode(
                        canonical_bytes(report.model_dump())
                    ).decode(),
                    image=image,
                    definition=definition,
                    definition_payload_base64=base64.b64encode(
                        canonical_bytes(definition.model_dump())
                    ).decode(),
                    spec_digest=digest(payload),
                    spec_payload_base64=base64.b64encode(payload).decode(),
                    created_by=actor.subject,
                    initiated_by=command.initiated_by,
                    created_at=self.registry.clock(),
                )
                con.execute(
                    insert(conversions).values(
                        zone=actor.zone,
                        id=record.id,
                        asset_id=target.id,
                        request_id=command.request_id,
                        request_digest=fingerprint,
                        document=record.model_dump_json(),
                    )
                )
                return record
        except IntegrityError:
            with self.engine.connect() as con:
                row = previous(con)
                if row:
                    return replay(row)
            raise RegistryError("NATIVE_WRITE_CONFLICT") from None

    def get(self, actor, conversion_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            doc = con.execute(
                select(conversions.c.document).where(
                    conversions.c.zone == actor.zone,
                    conversions.c.id == conversion_id,
                )
            ).scalar_one_or_none()
        if doc is None:
            raise RegistryError("NATIVE_CONVERSION_NOT_FOUND", 404)
        return self._decode(doc)

    def profile(self, actor, conversion_id):
        from nexus.services.product_agent_registry.native_execution import execution_profile

        return execution_profile(self.get(actor, conversion_id))

    def list(self, actor, asset_id, after=""):
        self._authorize(actor)
        self.registry.inventory.get(actor, asset_id)
        with self.engine.connect() as con:
            rows = (
                con.execute(
                    select(conversions.c.document)
                    .where(
                        conversions.c.zone == actor.zone,
                        conversions.c.asset_id == asset_id,
                        conversions.c.id > after,
                    )
                    .order_by(conversions.c.id)
                    .limit(6)
                )
                .scalars()
                .all()
            )
        items = [self._decode(doc) for doc in rows]
        return NativeConversionPage(
            items=items[:5], next_cursor=items[4].id if len(items) > 5 else None
        )
