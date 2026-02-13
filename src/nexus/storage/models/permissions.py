"""ReBAC (Relationship-Based Access Control) and Tiger Cache models.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


class ReBACTupleModel(Base):
    """Relationship tuple for ReBAC system.

    Stores (subject, relation, object) tuples representing relationships
    between entities in the authorization graph.
    """

    __tablename__ = "rebac_tuples"

    tuple_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    subject_zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    object_zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_relation: Mapped[str | None] = mapped_column(String(50), nullable=True)

    relation: Mapped[str] = mapped_column(String(50), nullable=False)

    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_rebac_zone_subject", "zone_id", "subject_type", "subject_id"),
        Index("idx_rebac_zone_object", "zone_id", "object_type", "object_id"),
        Index("idx_rebac_relation", "relation"),
        Index("idx_rebac_expires", "expires_at"),
        Index("idx_rebac_subject_relation", "subject_type", "subject_id", "subject_relation"),
        Index(
            "idx_rebac_permission_check",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "zone_id",
        ),
        Index(
            "idx_rebac_userset_lookup",
            "relation",
            "object_type",
            "object_id",
            "subject_relation",
            "zone_id",
        ),
        Index(
            "idx_rebac_object_expand",
            "object_type",
            "object_id",
            "relation",
            "zone_id",
        ),
        Index(
            "idx_rebac_alive_permission_check",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "zone_id",
            postgresql_where=text("expires_at IS NULL"),
        ),
        Index(
            "idx_rebac_alive_by_subject",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            postgresql_where=text("expires_at IS NULL"),
        ),
        Index(
            "idx_rebac_alive_zone_object",
            "zone_id",
            "object_type",
            "object_id",
            "relation",
            postgresql_where=text("expires_at IS NULL"),
        ),
        Index(
            "idx_rebac_alive_userset",
            "relation",
            "object_type",
            "object_id",
            "subject_relation",
            "zone_id",
            postgresql_where=text("expires_at IS NULL AND subject_relation IS NOT NULL"),
        ),
        Index(
            "idx_rebac_cross_zone_shares",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            postgresql_where=text(
                "relation IN ('shared-viewer', 'shared-editor', 'shared-owner') "
                "AND expires_at IS NULL"
            ),
        ),
        Index(
            "idx_rebac_permission_check_covering",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "zone_id",
            postgresql_include=["tuple_id", "expires_at", "created_at"],
            postgresql_where=text("expires_at IS NULL"),
        ),
    )


class ReBACNamespaceModel(Base):
    """Namespace configuration for ReBAC permission expansion."""

    __tablename__ = "rebac_namespaces"

    namespace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    config: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ReBACGroupClosureModel(Base):
    """Leopard-style transitive group closure for O(1) membership lookups."""

    __tablename__ = "rebac_group_closure"

    member_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    group_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    depth: Mapped[int] = mapped_column(Integer, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<ReBACGroupClosureModel("
            f"{self.member_type}:{self.member_id} -> "
            f"{self.group_type}:{self.group_id}, depth={self.depth})>"
        )


class ReBACChangelogModel(Base):
    """Change log for ReBAC tuple modifications."""

    __tablename__ = "rebac_changelog"

    change_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_type: Mapped[str] = mapped_column(String(10), nullable=False)

    tuple_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relation: Mapped[str | None] = mapped_column(String(50), nullable=True)
    object_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class ReBACVersionSequenceModel(Base):
    """Per-zone version sequence for ReBAC consistency tokens."""

    __tablename__ = "rebac_version_sequences"

    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__: tuple = ()


class FileSystemVersionSequenceModel(Base):
    """Per-zone version sequence for filesystem consistency tokens (Issue #1187)."""

    __tablename__ = "filesystem_version_sequences"

    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__: tuple = ()


class ReBACCheckCacheModel(Base):
    """Cache for ReBAC permission check results."""

    __tablename__ = "rebac_check_cache"

    cache_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(String(50), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)

    result: Mapped[bool] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index(
            "idx_rebac_cache_zone_check",
            "zone_id",
            "subject_type",
            "subject_id",
            "permission",
            "object_type",
            "object_id",
        ),
        Index(
            "idx_rebac_cache_check",
            "subject_type",
            "subject_id",
            "permission",
            "object_type",
            "object_id",
        ),
    )


class TigerResourceMapModel(Base):
    """Maps resource UUIDs to int64 IDs for Roaring Bitmap compatibility."""

    __tablename__ = "tiger_resource_map"

    resource_int_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", name="uq_tiger_resource"),
        Index("idx_tiger_resource_lookup", "resource_type", "resource_id"),
    )

    def __repr__(self) -> str:
        return f"<TigerResourceMapModel({self.resource_int_id}: {self.resource_type}:{self.resource_id})>"


class TigerCacheModel(Base):
    """Stores pre-materialized permissions as Roaring Bitmaps."""

    __tablename__ = "tiger_cache"

    cache_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)

    permission: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    bitmap_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "permission",
            "resource_type",
            "zone_id",
            name="uq_tiger_cache",
        ),
        Index(
            "idx_tiger_cache_lookup",
            "zone_id",
            "subject_type",
            "subject_id",
            "permission",
            "resource_type",
        ),
        Index("idx_tiger_cache_revision", "revision"),
    )

    def __repr__(self) -> str:
        return (
            f"<TigerCacheModel({self.subject_type}:{self.subject_id} "
            f"{self.permission} {self.resource_type}, rev={self.revision})>"
        )


class TigerCacheQueueModel(Base):
    """Queue for async background updates of Tiger Cache."""

    __tablename__ = "tiger_cache_queue"

    queue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)

    permission: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_tiger_queue_pending", "status", "priority", "created_at"),)

    def __repr__(self) -> str:
        return (
            f"<TigerCacheQueueModel(queue_id={self.queue_id}, "
            f"{self.subject_type}:{self.subject_id}, status={self.status})>"
        )


class TigerDirectoryGrantsModel(Base):
    """Tracks directory-level permission grants for Leopard-style expansion."""

    __tablename__ = "tiger_directory_grants"

    grant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)

    permission: Mapped[str] = mapped_column(String(50), nullable=False)
    directory_path: Mapped[str] = mapped_column(Text, nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    grant_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    include_future_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    expansion_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    expanded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "zone_id",
            "directory_path",
            "permission",
            "subject_type",
            "subject_id",
            name="uq_tiger_directory_grants",
        ),
        Index("idx_tiger_dir_grants_path_prefix", "zone_id", "directory_path"),
        Index("idx_tiger_dir_grants_subject", "zone_id", "subject_type", "subject_id"),
        Index("idx_tiger_dir_grants_pending", "expansion_status", "created_at"),
        Index("idx_tiger_dir_grants_lookup", "zone_id", "directory_path", "permission"),
    )

    def __repr__(self) -> str:
        return (
            f"<TigerDirectoryGrantsModel(grant_id={self.grant_id}, "
            f"{self.subject_type}:{self.subject_id}, "
            f"dir={self.directory_path}, status={self.expansion_status})>"
        )
