"""Idempotent storage schema invariants not fully represented by ORM metadata."""

from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from nexus.contracts.constants import ROOT_ZONE_ID

_ROOT_ZONE_SQL = ROOT_ZONE_ID.replace("'", "''")


def _column_names_by_table(inspector: Any, table_names: set[str]) -> dict[str, set[str]]:
    return {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in table_names
    }


def _column_needs_varchar_widen(
    inspector: Any,
    table_name: str,
    column_name: str,
    min_length: int,
) -> bool:
    for column in inspector.get_columns(table_name):
        if column["name"] != column_name:
            continue

        # Unit-test fakes only expose column names. Treat them as legacy so
        # the regression test can verify the repair SQL without a live PG DB.
        if "type" not in column:
            return True

        length = getattr(column["type"], "length", None)
        return length is not None and length < min_length
    return False


def _ensure_zone_column(
    conn: Any,
    columns_by_table: dict[str, set[str]],
    table_name: str,
    sql_type: str,
    *,
    backfill_from: str | None = None,
) -> None:
    columns = columns_by_table.get(table_name)
    if columns is None or "zone_id" in columns:
        return

    conn.execute(
        text(
            f"""
            ALTER TABLE {table_name}
            ADD COLUMN zone_id {sql_type} NOT NULL DEFAULT '{_ROOT_ZONE_SQL}'
            """
        )
    )
    columns.add("zone_id")

    if backfill_from and backfill_from in columns:
        conn.execute(
            text(
                f"""
                UPDATE {table_name}
                SET zone_id = COALESCE({backfill_from}, '{_ROOT_ZONE_SQL}')
                WHERE {backfill_from} IS NOT NULL
                """
            )
        )


def _ensure_tenant_column_default(
    conn: Any,
    columns_by_table: dict[str, set[str]],
    table_name: str,
) -> None:
    columns = columns_by_table.get(table_name)
    if columns is None or "tenant_id" not in columns:
        return

    # Older migrations left tenant_id NOT NULL, but current ORM writes zone_id.
    # A default keeps those legacy compatibility columns from rejecting inserts.
    conn.execute(
        text(
            f"""
            ALTER TABLE {table_name}
            ALTER COLUMN tenant_id SET DEFAULT '{_ROOT_ZONE_SQL}'
            """
        )
    )


def _ensure_rebac_id_lengths(conn: Any, inspector: Any, table_names: set[str]) -> None:
    for table_name in ("rebac_tuples", "rebac_changelog"):
        if table_name not in table_names:
            continue
        for column_name in ("subject_id", "object_id"):
            if _column_needs_varchar_widen(inspector, table_name, column_name, 255):
                conn.execute(
                    text(
                        f"""
                        ALTER TABLE {table_name}
                        ALTER COLUMN {column_name} TYPE VARCHAR(255)
                        """
                    )
                )


def _ensure_zone_indexes(conn: Any, columns_by_table: dict[str, set[str]]) -> None:
    file_path_columns = columns_by_table.get("file_paths", set())
    if {"zone_id", "virtual_path", "deleted_at"}.issubset(file_path_columns):
        conn.execute(
            text(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_zone_virtual_path
                ON file_paths (zone_id, virtual_path)
                WHERE deleted_at IS NULL
                """
            )
        )
        if "content_id" in file_path_columns:
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_content_id_zone
                    ON file_paths (content_id, zone_id)
                    """
                )
            )
        if {"path_id", "content_id", "size_bytes", "updated_at", "file_type"}.issubset(
            file_path_columns
        ):
            conn.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_file_paths_zone_path_covering
                    ON file_paths (zone_id, virtual_path)
                    INCLUDE (path_id, content_id, size_bytes, updated_at, file_type)
                    WHERE deleted_at IS NULL
                    """
                )
            )

    if "zone_id" in columns_by_table.get("rebac_changelog", set()):
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS ix_rebac_changelog_zone_id
                ON rebac_changelog (zone_id)
                """
            )
        )

    tiger_directory_columns = columns_by_table.get("tiger_directory_grants", set())
    if {"zone_id", "directory_path", "subject_type", "subject_id", "permission"}.issubset(
        tiger_directory_columns
    ):
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_tiger_dir_grants_path_prefix
                ON tiger_directory_grants (zone_id, directory_path text_pattern_ops)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_tiger_dir_grants_subject
                ON tiger_directory_grants (zone_id, subject_type, subject_id)
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_tiger_dir_grants_lookup
                ON tiger_directory_grants (zone_id, directory_path, permission)
                """
            )
        )

    if "zone_id" in columns_by_table.get("subscriptions", set()):
        conn.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_subscriptions_zone
                ON subscriptions (zone_id)
                """
            )
        )


def _ensure_tiger_directory_grants_constraint(
    conn: Any,
    columns_by_table: dict[str, set[str]],
) -> None:
    required = {"zone_id", "directory_path", "permission", "subject_type", "subject_id"}
    if not required.issubset(columns_by_table.get("tiger_directory_grants", set())):
        return

    conn.execute(
        text(
            """
            DO $$
            DECLARE
                current_columns text[];
            BEGIN
                SELECT array_agg(att.attname ORDER BY keys.ordinality)
                INTO current_columns
                FROM pg_constraint con
                JOIN unnest(con.conkey) WITH ORDINALITY AS keys(attnum, ordinality)
                  ON true
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid
                 AND att.attnum = keys.attnum
                WHERE con.conrelid = 'tiger_directory_grants'::regclass
                  AND con.conname = 'uq_tiger_directory_grants'
                  AND con.contype = 'u';

                IF current_columns IS DISTINCT FROM ARRAY[
                    'zone_id',
                    'directory_path',
                    'permission',
                    'subject_type',
                    'subject_id'
                ] THEN
                    IF current_columns IS NOT NULL THEN
                        ALTER TABLE tiger_directory_grants
                        DROP CONSTRAINT uq_tiger_directory_grants;
                    END IF;

                    ALTER TABLE tiger_directory_grants
                    ADD CONSTRAINT uq_tiger_directory_grants
                    UNIQUE (
                        zone_id,
                        directory_path,
                        permission,
                        subject_type,
                        subject_id
                    );
                END IF;
            END $$;
            """
        )
    )


def _ensure_mcl_sequence(conn: Any) -> None:
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


def ensure_postgres_schema_invariants(engine: Engine) -> None:
    """Repair PostgreSQL invariants that ``Base.metadata.create_all`` cannot express.

    Alembic is the schema source of truth, but some legacy/fresh-init paths
    created tables from ORM metadata and then stamped migrations as applied.
    Validate those invariants explicitly before the server accepts writes.
    """
    if engine.dialect.name != "postgresql":
        return

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    columns_by_table = _column_names_by_table(inspector, table_names)

    with engine.begin() as conn:
        _ensure_zone_column(conn, columns_by_table, "file_paths", "VARCHAR(255)")
        _ensure_zone_column(conn, columns_by_table, "rebac_changelog", "VARCHAR(255)")
        _ensure_zone_column(
            conn,
            columns_by_table,
            "tiger_directory_grants",
            "VARCHAR(255)",
            backfill_from="tenant_id",
        )
        _ensure_zone_column(
            conn,
            columns_by_table,
            "subscriptions",
            "VARCHAR(36)",
            backfill_from="tenant_id",
        )

        _ensure_tenant_column_default(conn, columns_by_table, "tiger_directory_grants")
        _ensure_tenant_column_default(conn, columns_by_table, "subscriptions")
        _ensure_rebac_id_lengths(conn, inspector, table_names)
        _ensure_zone_indexes(conn, columns_by_table)
        _ensure_tiger_directory_grants_constraint(conn, columns_by_table)

        if "metadata_change_log" in table_names:
            _ensure_mcl_sequence(conn)
