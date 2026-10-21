"""Immutable, organization-scoped hosted artifacts; imports never publish or raise grade."""

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

from nexus.contracts.agent_hosted import HostedArtifact, HostedArtifactPage, RegisterHostedArtifact
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.dify_import import compile_dify_text
from nexus.services.product_agent_registry.store import RegistryError, digest

metadata = MetaData()
artifacts = Table(
    "product_agent_hosted_artifacts",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("asset_id", String(150), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "request_id"),
)


def initialize_hosted_schema(engine):
    """Explicit, additive operator action. Never invoked by GET or startup."""
    metadata.create_all(engine)


class HostedArtifacts:
    def __init__(self, registry):
        self.registry, self.engine = registry, registry.engine

    def _authorize(self, actor):
        self.registry.inventory._authorize(actor)
        if not set(metadata.tables).issubset(inspect(self.engine).get_table_names()):
            raise RegistryError("HOSTED_SCHEMA_REQUIRED", 503)

    def register(self, actor, command: RegisterHostedArtifact):
        self._authorize(actor)
        fingerprint = digest(
            canonical_bytes({"actor": actor.subject, "command": command.model_dump()})
        )
        plan = (
            compile_dify_text(command.dify_source.encode())
            if command.dify_source is not None
            else None
        )
        image = command.image
        if plan and (
            image.source_digest != plan.source_digest
            or image.model_name != plan.source_model
            or image.max_output_tokens != (plan.max_tokens or 128)
        ):
            raise RegistryError("HOSTED_SOURCE_MISMATCH", 400)
        if image.image_id == "sha256:" + "0" * 64:
            raise RegistryError("HOSTED_BUILT_IMAGE_REQUIRED", 400)
        payload = canonical_bytes(
            {"image": image.model_dump(), "dify_plan": plan.model_dump() if plan else None}
        )
        if len(payload) > 500000:
            raise RegistryError("HOSTED_SPEC_LIMIT", 413)
        spec = digest(payload)

        def previous(con):
            return (
                con.execute(
                    select(artifacts).where(
                        artifacts.c.zone == actor.zone, artifacts.c.request_id == command.request_id
                    )
                )
                .mappings()
                .first()
            )

        def replay(row):
            if row["request_digest"] != fingerprint:
                raise RegistryError("HOSTED_IDEMPOTENCY_CONFLICT")
            return HostedArtifact.model_validate_json(row["document"])

        try:
            with self.registry.inventory._transaction() as con:
                row = previous(con)
                if row:
                    return replay(row)
                asset = self.registry.inventory._get(con, actor.zone, command.asset_id, lock=True)
                if (
                    asset.status in ("suspended", "merged")
                    or not asset.metadata.owner_user_id
                    or asset.metadata.hosting != "internal_hosted"
                    or asset.metadata_digest != command.metadata_digest
                    or image.model_resource not in asset.metadata.model_paths
                ):
                    raise RegistryError("HOSTED_ASSET_NOT_ADMITTED")
                expected_platform = "dify" if plan else "langgraph"
                if asset.metadata.source_platform != expected_platform:
                    raise RegistryError("HOSTED_PLATFORM_MISMATCH", 400)
                record = HostedArtifact(
                    id=str(uuid4()),
                    zone=actor.zone,
                    asset_id=asset.id,
                    metadata_digest=asset.metadata_digest,
                    spec_digest=spec,
                    spec_payload_base64=base64.b64encode(payload).decode(),
                    image=image,
                    dify_plan=plan,
                    created_by=actor.subject,
                    initiated_by=command.initiated_by,
                    created_at=self.registry.clock(),
                )
                con.execute(
                    insert(artifacts).values(
                        zone=actor.zone,
                        id=record.id,
                        asset_id=asset.id,
                        request_id=command.request_id,
                        request_digest=fingerprint,
                        document=record.model_dump_json(),
                    )
                )
                return record
        except IntegrityError:
            # Concurrent exact request returns the winner; never runs/builds an image here.
            with self.engine.connect() as con:
                row = previous(con)
                if row:
                    return replay(row)
            raise RegistryError("HOSTED_WRITE_CONFLICT") from None

    def get(self, actor, artifact_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            doc = con.execute(
                select(artifacts.c.document).where(
                    artifacts.c.zone == actor.zone, artifacts.c.id == artifact_id
                )
            ).scalar_one_or_none()
        if doc is None:
            raise RegistryError("HOSTED_ARTIFACT_NOT_FOUND", 404)
        return HostedArtifact.model_validate_json(doc)

    def list(self, actor, asset_id, after=""):
        self._authorize(actor)
        self.registry.inventory.get(actor, asset_id)
        with self.engine.connect() as con:
            rows = (
                con.execute(
                    select(artifacts.c.document)
                    .where(
                        artifacts.c.zone == actor.zone,
                        artifacts.c.asset_id == asset_id,
                        artifacts.c.id > after,
                    )
                    .order_by(artifacts.c.id)
                    .limit(6)
                )
                .scalars()
                .all()
            )
        items = [HostedArtifact.model_validate_json(doc) for doc in rows]
        return HostedArtifactPage(
            items=items[:5], next_cursor=items[4].id if len(items) > 5 else None
        )
