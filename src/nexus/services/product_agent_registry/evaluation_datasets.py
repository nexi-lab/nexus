"""Immutable evaluation inputs. Authenticated recording never executes or promotes an asset."""

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

from nexus.contracts.agent_evaluation import (
    EvaluationDataset,
    EvaluationDatasetPage,
    RegisterEvaluationDataset,
)
from nexus.contracts.product_agent_registry import canonical_bytes
from nexus.services.product_agent_registry.store import RegistryError, digest

metadata = MetaData()
datasets = Table(
    "product_agent_evaluation_datasets",
    metadata,
    Column("zone", String(255), primary_key=True),
    Column("id", String(150), primary_key=True),
    Column("asset_id", String(150), nullable=False),
    Column("request_id", String(128), nullable=False),
    Column("request_digest", String(71), nullable=False),
    Column("name", String(200), nullable=False),
    Column("version", String(128), nullable=False),
    Column("document", Text, nullable=False),
    UniqueConstraint("zone", "request_id"),
    UniqueConstraint("zone", "asset_id", "name", "version"),
)
RUBRIC = {
    "id": "exact-strip-v1",
    "comparison": "strip-leading-trailing-ASCII-whitespace-U0009-U000D-U0020-exact-case-sensitive",
    "denominator": "all-cases-including-errors",
    "aggregation": "passed-cases/total-cases",
}


def initialize_evaluation_schema(engine):
    metadata.create_all(engine)


class EvaluationDatasets:
    def __init__(self, registry):
        self.registry, self.engine = registry, registry.engine

    def _authorize(self, actor):
        self.registry.inventory._authorize(actor)
        if not set(metadata.tables).issubset(inspect(self.engine).get_table_names()):
            raise RegistryError("EVALUATION_SCHEMA_REQUIRED", 503)

    def register(self, actor, command: RegisterEvaluationDataset):
        self._authorize(actor)
        payload = canonical_bytes(command.dataset.model_dump())
        if len(payload) > 512000:
            raise RegistryError("EVALUATION_DATASET_LIMIT", 413)
        fingerprint = digest(
            canonical_bytes({"actor": actor.subject, "command": command.model_dump()})
        )

        def previous(con):
            return (
                con.execute(
                    select(datasets).where(
                        datasets.c.zone == actor.zone, datasets.c.request_id == command.request_id
                    )
                )
                .mappings()
                .first()
            )

        def replay(row):
            if row["request_digest"] != fingerprint:
                raise RegistryError("EVALUATION_IDEMPOTENCY_CONFLICT")
            return EvaluationDataset.model_validate_json(row["document"])

        try:
            with self.registry.inventory._transaction() as con:
                row = previous(con)
                if row:
                    return replay(row)
                asset = self.registry.inventory._get(con, actor.zone, command.asset_id, lock=True)
                if (
                    asset.status in ("suspended", "merged")
                    or asset.metadata_digest != command.metadata_digest
                    or not asset.metadata.owner_user_id
                ):
                    raise RegistryError("EVALUATION_ASSET_NOT_ADMITTED")
                record = EvaluationDataset(
                    id=str(uuid4()),
                    zone=actor.zone,
                    asset_id=asset.id,
                    metadata_digest=asset.metadata_digest,
                    dataset_digest=digest(payload),
                    rubric_digest=digest(canonical_bytes(RUBRIC)),
                    dataset=command.dataset,
                    created_by=actor.subject,
                    initiated_by=command.initiated_by,
                    created_at=self.registry.clock(),
                )
                con.execute(
                    insert(datasets).values(
                        zone=actor.zone,
                        id=record.id,
                        asset_id=asset.id,
                        request_id=command.request_id,
                        request_digest=fingerprint,
                        name=command.dataset.name,
                        version=command.dataset.version,
                        document=record.model_dump_json(),
                    )
                )
                return record
        except IntegrityError:
            with self.engine.connect() as con:
                row = previous(con)
                if row:
                    return replay(row)
            raise RegistryError("EVALUATION_VERSION_CONFLICT") from None

    def get(self, actor, dataset_id):
        self._authorize(actor)
        with self.engine.connect() as con:
            document = con.execute(
                select(datasets.c.document).where(
                    datasets.c.zone == actor.zone, datasets.c.id == dataset_id
                )
            ).scalar_one_or_none()
        if document is None:
            raise RegistryError("EVALUATION_DATASET_NOT_FOUND", 404)
        return EvaluationDataset.model_validate_json(document)

    def list(self, actor, asset_id, after=""):
        self._authorize(actor)
        self.registry.inventory.get(actor, asset_id)
        with self.engine.connect() as con:
            documents = (
                con.execute(
                    select(datasets.c.document)
                    .where(
                        datasets.c.zone == actor.zone,
                        datasets.c.asset_id == asset_id,
                        datasets.c.id > after,
                    )
                    .order_by(datasets.c.id)
                    .limit(6)
                )
                .scalars()
                .all()
            )
        items = [EvaluationDataset.model_validate_json(doc) for doc in documents]
        return EvaluationDatasetPage(
            items=items[:5], next_cursor=items[4].id if len(items) > 5 else None
        )
