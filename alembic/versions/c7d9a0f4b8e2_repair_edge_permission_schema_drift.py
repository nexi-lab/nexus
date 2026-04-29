"""Repair edge permission schema drift missed by zone bootstrap.

Revision ID: c7d9a0f4b8e2
Revises: b6f4a8d9c2e1
Create Date: 2026-04-29
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d9a0f4b8e2"
down_revision: str | Sequence[str] | None = "b6f4a8d9c2e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROOT_ZONE_ID = "root"


def _table_columns(inspector: Any, table_name: str) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _add_zone_column(
    table_name: str,
    columns: set[str],
    sql_type: sa.TypeEngine,
    *,
    backfill_from: str | None = None,
) -> None:
    if "zone_id" in columns:
        return

    op.add_column(
        table_name,
        sa.Column("zone_id", sql_type, nullable=False, server_default=ROOT_ZONE_ID),
    )
    columns.add("zone_id")

    if backfill_from and backfill_from in columns:
        op.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET zone_id = COALESCE({backfill_from}, :root_zone)
                WHERE {backfill_from} IS NOT NULL
                """
            ).bindparams(root_zone=ROOT_ZONE_ID)
        )


def _set_tenant_default(bind: Any, table_name: str, columns: set[str]) -> None:
    if bind.dialect.name != "postgresql" or "tenant_id" not in columns:
        return

    op.execute(
        sa.text(
            f"""
            ALTER TABLE {table_name}
            ALTER COLUMN tenant_id SET DEFAULT 'root'
            """
        )
    )


def _drop_not_null_if_needed(
    bind: Any,
    inspector: Any,
    table_name: str,
    column_name: str,
) -> None:
    if bind.dialect.name != "postgresql":
        return

    column = _table_columns(inspector, table_name).get(column_name)
    if not column or column.get("nullable") is not False:
        return

    op.execute(
        sa.text(
            f"""
            ALTER TABLE {table_name}
            ALTER COLUMN {column_name} DROP NOT NULL
            """
        )
    )


def _widen_varchar_if_needed(
    bind: Any,
    inspector: Any,
    table_name: str,
    column_name: str,
    target_length: int,
) -> None:
    if bind.dialect.name != "postgresql":
        return

    column = _table_columns(inspector, table_name).get(column_name)
    if not column:
        return

    current_length = getattr(column["type"], "length", None)
    if current_length is None or current_length >= target_length:
        return

    op.execute(
        sa.text(
            f"""
            ALTER TABLE {table_name}
            ALTER COLUMN {column_name} TYPE VARCHAR({target_length})
            """
        )
    )


def _replace_tiger_cache_shape(bind: Any, columns: set[str]) -> None:
    if bind.dialect.name != "postgresql":
        return
    if not {"subject_type", "subject_id", "permission", "resource_type", "zone_id"}.issubset(
        columns
    ):
        return

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                current_columns text[];
                current_index_columns text[];
            BEGIN
                SELECT array_agg(att.attname ORDER BY keys.ordinality)
                INTO current_columns
                FROM pg_constraint con
                JOIN unnest(con.conkey) WITH ORDINALITY AS keys(attnum, ordinality)
                  ON true
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid
                 AND att.attnum = keys.attnum
                WHERE con.conrelid = 'tiger_cache'::regclass
                  AND con.conname = 'uq_tiger_cache'
                  AND con.contype = 'u';

                IF current_columns IS DISTINCT FROM ARRAY[
                    'subject_type',
                    'subject_id',
                    'permission',
                    'resource_type',
                    'zone_id'
                ] THEN
                    IF current_columns IS NOT NULL THEN
                        ALTER TABLE tiger_cache
                        DROP CONSTRAINT uq_tiger_cache;
                    END IF;

                    ALTER TABLE tiger_cache
                    ADD CONSTRAINT uq_tiger_cache
                    UNIQUE (
                        subject_type,
                        subject_id,
                        permission,
                        resource_type,
                        zone_id
                    );
                END IF;

                SELECT array_agg(att.attname ORDER BY keys.ordinality)
                INTO current_index_columns
                FROM pg_class idx
                JOIN pg_index ind
                  ON ind.indexrelid = idx.oid
                JOIN unnest(ind.indkey) WITH ORDINALITY AS keys(attnum, ordinality)
                  ON true
                JOIN pg_attribute att
                  ON att.attrelid = ind.indrelid
                 AND att.attnum = keys.attnum
                WHERE idx.relname = 'idx_tiger_cache_lookup'
                  AND ind.indrelid = 'tiger_cache'::regclass;

                IF current_index_columns IS DISTINCT FROM ARRAY[
                    'zone_id',
                    'subject_type',
                    'subject_id',
                    'permission',
                    'resource_type'
                ] THEN
                    DROP INDEX IF EXISTS idx_tiger_cache_lookup;
                    CREATE INDEX idx_tiger_cache_lookup
                    ON tiger_cache (
                        zone_id,
                        subject_type,
                        subject_id,
                        permission,
                        resource_type
                    );
                END IF;
            END $$;
            """
        )
    )


def _replace_group_closure_shape(bind: Any, columns: set[str]) -> None:
    if bind.dialect.name != "postgresql":
        return
    if not {"member_type", "member_id", "group_type", "group_id", "zone_id"}.issubset(columns):
        return

    op.execute(
        sa.text(
            """
            DO $$
            DECLARE
                current_columns text[];
                member_index_columns text[];
                group_index_columns text[];
            BEGIN
                SELECT array_agg(att.attname ORDER BY keys.ordinality)
                INTO current_columns
                FROM pg_constraint con
                JOIN unnest(con.conkey) WITH ORDINALITY AS keys(attnum, ordinality)
                  ON true
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid
                 AND att.attnum = keys.attnum
                WHERE con.conrelid = 'rebac_group_closure'::regclass
                  AND con.contype = 'p';

                IF current_columns IS DISTINCT FROM ARRAY[
                    'member_type',
                    'member_id',
                    'group_type',
                    'group_id',
                    'zone_id'
                ] THEN
                    ALTER TABLE rebac_group_closure
                    DROP CONSTRAINT IF EXISTS rebac_group_closure_pkey;

                    ALTER TABLE rebac_group_closure
                    ADD PRIMARY KEY (member_type, member_id, group_type, group_id, zone_id);
                END IF;

                SELECT array_agg(att.attname ORDER BY keys.ordinality)
                INTO member_index_columns
                FROM pg_class idx
                JOIN pg_index ind
                  ON ind.indexrelid = idx.oid
                JOIN unnest(ind.indkey) WITH ORDINALITY AS keys(attnum, ordinality)
                  ON true
                JOIN pg_attribute att
                  ON att.attrelid = ind.indrelid
                 AND att.attnum = keys.attnum
                WHERE idx.relname = 'idx_closure_member'
                  AND ind.indrelid = 'rebac_group_closure'::regclass;

                IF member_index_columns IS DISTINCT FROM ARRAY[
                    'zone_id',
                    'member_type',
                    'member_id'
                ] THEN
                    DROP INDEX IF EXISTS idx_closure_member;
                    CREATE INDEX idx_closure_member
                    ON rebac_group_closure (zone_id, member_type, member_id);
                END IF;

                SELECT array_agg(att.attname ORDER BY keys.ordinality)
                INTO group_index_columns
                FROM pg_class idx
                JOIN pg_index ind
                  ON ind.indexrelid = idx.oid
                JOIN unnest(ind.indkey) WITH ORDINALITY AS keys(attnum, ordinality)
                  ON true
                JOIN pg_attribute att
                  ON att.attrelid = ind.indrelid
                 AND att.attnum = keys.attnum
                WHERE idx.relname = 'idx_closure_group'
                  AND ind.indrelid = 'rebac_group_closure'::regclass;

                IF group_index_columns IS DISTINCT FROM ARRAY[
                    'zone_id',
                    'group_type',
                    'group_id'
                ] THEN
                    DROP INDEX IF EXISTS idx_closure_group;
                    CREATE INDEX idx_closure_group
                    ON rebac_group_closure (zone_id, group_type, group_id);
                END IF;
            END $$;
            """
        )
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "file_paths" in table_names:
        _drop_not_null_if_needed(bind, inspector, "file_paths", "backend_id")
        _drop_not_null_if_needed(bind, inspector, "file_paths", "physical_path")
        _widen_varchar_if_needed(bind, inspector, "file_paths", "content_id", 255)
        _widen_varchar_if_needed(bind, inspector, "file_paths", "indexed_content_id", 255)

    if "tiger_cache" in table_names:
        tiger_cache_columns = set(_table_columns(inspector, "tiger_cache"))
        _add_zone_column(
            "tiger_cache",
            tiger_cache_columns,
            sa.String(255),
            backfill_from="tenant_id",
        )
        _set_tenant_default(bind, "tiger_cache", tiger_cache_columns)
        _replace_tiger_cache_shape(bind, tiger_cache_columns)

    if "tiger_cache_queue" in table_names:
        queue_columns = set(_table_columns(inspector, "tiger_cache_queue"))
        _add_zone_column(
            "tiger_cache_queue",
            queue_columns,
            sa.String(255),
            backfill_from="tenant_id",
        )
        _set_tenant_default(bind, "tiger_cache_queue", queue_columns)

    if "rebac_group_closure" in table_names:
        closure_columns = set(_table_columns(inspector, "rebac_group_closure"))
        _add_zone_column(
            "rebac_group_closure",
            closure_columns,
            sa.String(255),
            backfill_from="tenant_id",
        )
        _set_tenant_default(bind, "rebac_group_closure", closure_columns)
        _replace_group_closure_shape(bind, closure_columns)


def downgrade() -> None:
    """No destructive downgrade for a compatibility repair migration."""
