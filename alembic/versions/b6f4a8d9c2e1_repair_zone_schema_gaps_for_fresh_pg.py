"""Repair zone schema gaps left by migration-only database bootstrap.

Revision ID: b6f4a8d9c2e1
Revises: 3b2a1c5d7e8f
Create Date: 2026-04-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6f4a8d9c2e1"
down_revision: str | Sequence[str] | None = "3b2a1c5d7e8f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROOT_ZONE_ID = "root"


def _table_columns(inspector: Any, table_name: str) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _table_indexes(inspector: Any, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


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


def _widen_varchar_if_needed(
    bind: Any,
    inspector: Any,
    table_name: str,
    column_name: str,
    target_length: int,
) -> None:
    if bind.dialect.name != "postgresql":
        return

    columns = _table_columns(inspector, table_name)
    column = columns.get(column_name)
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


def _create_file_path_indexes(bind: Any, indexes: set[str], columns: set[str]) -> None:
    if not {"zone_id", "virtual_path", "deleted_at"}.issubset(columns):
        return

    if "uq_zone_virtual_path" not in indexes:
        where = sa.text("deleted_at IS NULL")
        kwargs: dict[str, Any] = {"postgresql_where": where}
        if bind.dialect.name == "sqlite":
            kwargs = {"sqlite_where": where}
        op.create_index(
            "uq_zone_virtual_path",
            "file_paths",
            ["zone_id", "virtual_path"],
            unique=True,
            **kwargs,
        )

    if "idx_content_id_zone" not in indexes and "content_id" in columns:
        op.create_index("idx_content_id_zone", "file_paths", ["content_id", "zone_id"])

    include_columns = {"path_id", "content_id", "size_bytes", "updated_at", "file_type"}
    if "idx_file_paths_zone_path_covering" in indexes or not include_columns.issubset(columns):
        return

    where = sa.text("deleted_at IS NULL")
    if bind.dialect.name == "postgresql":
        op.create_index(
            "idx_file_paths_zone_path_covering",
            "file_paths",
            ["zone_id", "virtual_path"],
            postgresql_include=[
                "path_id",
                "content_id",
                "size_bytes",
                "updated_at",
                "file_type",
            ],
            postgresql_where=where,
        )
    else:
        op.create_index(
            "idx_file_paths_zone_path_covering",
            "file_paths",
            ["zone_id", "virtual_path"],
            sqlite_where=where,
        )


def _replace_tiger_directory_indexes(bind: Any, indexes: set[str], columns: set[str]) -> None:
    if not {"zone_id", "directory_path", "subject_type", "subject_id", "permission"}.issubset(
        columns
    ):
        return

    for index_name in (
        "idx_tiger_dir_grants_path_prefix",
        "idx_tiger_dir_grants_subject",
        "idx_tiger_dir_grants_lookup",
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name="tiger_directory_grants")

    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                CREATE INDEX idx_tiger_dir_grants_path_prefix
                ON tiger_directory_grants (zone_id, directory_path text_pattern_ops)
                """
            )
        )
    else:
        op.create_index(
            "idx_tiger_dir_grants_path_prefix",
            "tiger_directory_grants",
            ["zone_id", "directory_path"],
        )

    op.create_index(
        "idx_tiger_dir_grants_subject",
        "tiger_directory_grants",
        ["zone_id", "subject_type", "subject_id"],
    )
    op.create_index(
        "idx_tiger_dir_grants_lookup",
        "tiger_directory_grants",
        ["zone_id", "directory_path", "permission"],
    )


def _replace_tiger_directory_constraint(bind: Any, columns: set[str]) -> None:
    if bind.dialect.name != "postgresql":
        return
    if not {"zone_id", "directory_path", "permission", "subject_type", "subject_id"}.issubset(
        columns
    ):
        return

    op.execute(
        sa.text(
            """
            ALTER TABLE tiger_directory_grants
            DROP CONSTRAINT IF EXISTS uq_tiger_directory_grants
            """
        )
    )
    op.create_unique_constraint(
        "uq_tiger_directory_grants",
        "tiger_directory_grants",
        ["zone_id", "directory_path", "permission", "subject_type", "subject_id"],
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "file_paths" in table_names:
        file_path_columns = set(_table_columns(inspector, "file_paths"))
        _add_zone_column("file_paths", file_path_columns, sa.String(255))
        _create_file_path_indexes(bind, _table_indexes(inspector, "file_paths"), file_path_columns)

    if "rebac_changelog" in table_names:
        changelog_columns = set(_table_columns(inspector, "rebac_changelog"))
        _add_zone_column("rebac_changelog", changelog_columns, sa.String(255))
        if "ix_rebac_changelog_zone_id" not in _table_indexes(inspector, "rebac_changelog"):
            op.create_index("ix_rebac_changelog_zone_id", "rebac_changelog", ["zone_id"])

    for table_name in ("rebac_tuples", "rebac_changelog"):
        if table_name not in table_names:
            continue
        _widen_varchar_if_needed(bind, inspector, table_name, "subject_id", 255)
        _widen_varchar_if_needed(bind, inspector, table_name, "object_id", 255)

    if "tiger_directory_grants" in table_names:
        grants_columns = set(_table_columns(inspector, "tiger_directory_grants"))
        _add_zone_column(
            "tiger_directory_grants",
            grants_columns,
            sa.String(255),
            backfill_from="tenant_id",
        )
        if bind.dialect.name == "postgresql" and "tenant_id" in grants_columns:
            op.execute(
                sa.text(
                    """
                    ALTER TABLE tiger_directory_grants
                    ALTER COLUMN tenant_id SET DEFAULT 'root'
                    """
                )
            )
        _replace_tiger_directory_indexes(
            bind,
            _table_indexes(inspector, "tiger_directory_grants"),
            grants_columns,
        )
        _replace_tiger_directory_constraint(bind, grants_columns)

    if "subscriptions" in table_names:
        subscription_columns = set(_table_columns(inspector, "subscriptions"))
        _add_zone_column(
            "subscriptions",
            subscription_columns,
            sa.String(36),
            backfill_from="tenant_id",
        )
        if bind.dialect.name == "postgresql" and "tenant_id" in subscription_columns:
            op.execute(
                sa.text(
                    """
                    ALTER TABLE subscriptions
                    ALTER COLUMN tenant_id SET DEFAULT 'root'
                    """
                )
            )
        if "idx_subscriptions_zone" not in _table_indexes(inspector, "subscriptions"):
            op.create_index("idx_subscriptions_zone", "subscriptions", ["zone_id"])


def downgrade() -> None:
    """No destructive downgrade for a compatibility repair migration."""
