"""Rename content_hash columns to content_id (and related indexes).

Part of the refactor that unifies ``etag`` (Rust) and ``content_hash``
(Python) under the canonical name ``content_id``. This migration renames
the SQL columns + indexes so the ORM models match the database schema.

Affected tables:
    - file_paths.content_hash         -> content_id
    - file_paths.indexed_content_hash -> indexed_content_id
    - memories.content_hash           -> content_id
    - upload_sessions.content_hash    -> content_id
    - version_history.content_hash    -> content_id
    - document_skeleton.skeleton_content_hash -> skeleton_content_id
    - lineage_reverse_index.upstream_etag    -> upstream_content_id

Indexes renamed:
    - idx_file_paths_content_hash      -> idx_file_paths_content_id
    - idx_content_hash_zone            -> idx_content_id_zone
    - idx_version_history_content_hash -> idx_version_history_content_id
    - idx_file_paths_zone_path_covering: dropped + recreated because its
      ``postgresql_include=[..., "content_hash", ...]`` covering list
      references the old column name.

Revision ID: 2163141d44c5
Revises: 04188c0bbb28
Create Date: 2026-04-28
"""

from __future__ import annotations

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision = "2163141d44c5"
down_revision = "04188c0bbb28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Some indexes are created only by Base.metadata.create_all() (newer ORM
    # additions that never had a dedicated migration), so we only drop them
    # if they actually exist. The covering index is a special case — it
    # references content_hash inside its postgresql_include list, so it
    # must be dropped + re-created if present.
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(bind)
    file_paths_indexes = {idx["name"] for idx in inspector.get_indexes("file_paths")}
    version_history_indexes = {idx["name"] for idx in inspector.get_indexes("version_history")}
    table_names = set(inspector.get_table_names())

    # ---------------------------------------------------------------------
    # 1. Drop indexes that reference the old column names BEFORE renaming
    #    the columns. Use conditional drops because some indexes may or
    #    may not exist depending on whether the schema came from migrations
    #    only vs. ``Base.metadata.create_all()``.
    # ---------------------------------------------------------------------
    had_content_hash_zone = "idx_content_hash_zone" in file_paths_indexes
    had_zone_path_covering = "idx_file_paths_zone_path_covering" in file_paths_indexes

    if "idx_file_paths_content_hash" in file_paths_indexes:
        op.drop_index("idx_file_paths_content_hash", table_name="file_paths")
    if had_content_hash_zone:
        op.drop_index("idx_content_hash_zone", table_name="file_paths")
    if had_zone_path_covering:
        op.drop_index("idx_file_paths_zone_path_covering", table_name="file_paths")
    if "idx_version_history_content_hash" in version_history_indexes:
        op.drop_index("idx_version_history_content_hash", table_name="version_history")

    # ---------------------------------------------------------------------
    # 2. Rename columns. ``upload_sessions`` is ORM-only (never had a
    #    dedicated migration), so guard its alter with a table check —
    #    the table only exists when the schema was bootstrapped via
    #    ``Base.metadata.create_all()``.
    # ---------------------------------------------------------------------
    op.alter_column("file_paths", "content_hash", new_column_name="content_id")
    op.alter_column("file_paths", "indexed_content_hash", new_column_name="indexed_content_id")
    op.alter_column("memories", "content_hash", new_column_name="content_id")
    if "upload_sessions" in table_names:
        op.alter_column("upload_sessions", "content_hash", new_column_name="content_id")
    op.alter_column("version_history", "content_hash", new_column_name="content_id")
    op.alter_column(
        "document_skeleton",
        "skeleton_content_hash",
        new_column_name="skeleton_content_id",
    )
    if "lineage_reverse_index" in table_names:
        op.alter_column(
            "lineage_reverse_index",
            "upstream_etag",
            new_column_name="upstream_content_id",
        )

    # ---------------------------------------------------------------------
    # 3. Re-create indexes against the new column names. Only re-create the
    #    ORM-only indexes (idx_content_id_zone, idx_file_paths_zone_path_covering)
    #    if they existed under the old names — keeps the migration idempotent
    #    against schemas that came purely from historical migrations.
    # ---------------------------------------------------------------------
    op.create_index("idx_file_paths_content_id", "file_paths", ["content_id"])
    op.create_index("idx_version_history_content_id", "version_history", ["content_id"])

    if had_content_hash_zone:
        op.create_index("idx_content_id_zone", "file_paths", ["content_id", "zone_id"])

    if had_zone_path_covering:
        if is_postgres:
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
                postgresql_where=text("deleted_at IS NULL"),
            )
        else:
            # SQLite has no INCLUDE; reproduce the equivalent partial index without it.
            op.create_index(
                "idx_file_paths_zone_path_covering",
                "file_paths",
                ["zone_id", "virtual_path"],
                sqlite_where=text("deleted_at IS NULL"),
            )


def downgrade() -> None:
    bind = op.get_bind()

    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(bind)
    file_paths_indexes = {idx["name"] for idx in inspector.get_indexes("file_paths")}
    version_history_indexes = {idx["name"] for idx in inspector.get_indexes("version_history")}
    table_names = set(inspector.get_table_names())

    # Drop new-name indexes (conditionally, mirroring upgrade())
    if "idx_file_paths_content_id" in file_paths_indexes:
        op.drop_index("idx_file_paths_content_id", table_name="file_paths")
    if "idx_content_id_zone" in file_paths_indexes:
        op.drop_index("idx_content_id_zone", table_name="file_paths")
    if "idx_file_paths_zone_path_covering" in file_paths_indexes:
        op.drop_index("idx_file_paths_zone_path_covering", table_name="file_paths")
    if "idx_version_history_content_id" in version_history_indexes:
        op.drop_index("idx_version_history_content_id", table_name="version_history")

    # Reverse the column renames
    if "lineage_reverse_index" in table_names:
        op.alter_column(
            "lineage_reverse_index",
            "upstream_content_id",
            new_column_name="upstream_etag",
        )
    op.alter_column(
        "document_skeleton",
        "skeleton_content_id",
        new_column_name="skeleton_content_hash",
    )
    op.alter_column("version_history", "content_id", new_column_name="content_hash")
    if "upload_sessions" in table_names:
        op.alter_column("upload_sessions", "content_id", new_column_name="content_hash")
    op.alter_column("memories", "content_id", new_column_name="content_hash")
    op.alter_column("file_paths", "indexed_content_id", new_column_name="indexed_content_hash")
    op.alter_column("file_paths", "content_id", new_column_name="content_hash")

    # Re-create historical-migration-era indexes only.
    # idx_content_hash_zone and idx_file_paths_zone_path_covering are
    # ORM-only (no dedicated migration creates them); they will be
    # re-created automatically by ``Base.metadata.create_all()`` on
    # next boot via the model definitions.
    op.create_index("idx_file_paths_content_hash", "file_paths", ["content_hash"])
    op.create_index("idx_version_history_content_hash", "version_history", ["content_hash"])
