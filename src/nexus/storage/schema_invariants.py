"""Idempotent storage schema invariants not fully represented by ORM metadata."""

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def ensure_postgres_schema_invariants(engine: Engine) -> None:
    """Repair PostgreSQL invariants that ``Base.metadata.create_all`` cannot express.

    Alembic is the schema source of truth, but some legacy/fresh-init paths
    created tables from ORM metadata and then stamped migrations as applied.
    PostgreSQL sequences are not represented in that metadata for all dialects,
    so validate them explicitly before the server starts accepting writes.
    """
    if engine.dialect.name != "postgresql":
        return

    inspector = inspect(engine)
    if "metadata_change_log" not in inspector.get_table_names():
        return

    with engine.begin() as conn:
        conn.execute(text("CREATE SEQUENCE IF NOT EXISTS mcl_sequence_number_seq"))
        conn.execute(
            text(
                """
                SELECT setval(
                    'mcl_sequence_number_seq',
                    COALESCE((SELECT MAX(sequence_number) FROM metadata_change_log), 0) + 1,
                    false
                )
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER TABLE metadata_change_log
                ALTER COLUMN sequence_number SET DEFAULT nextval('mcl_sequence_number_seq')
                """
            )
        )
        conn.execute(
            text(
                """
                ALTER SEQUENCE mcl_sequence_number_seq
                OWNED BY metadata_change_log.sequence_number
                """
            )
        )
