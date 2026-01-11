"""HNSW index configuration with auto-tuning based on dataset size.

This module provides configurable HNSW parameters for pgvector with
automatic optimization based on the number of vectors in the dataset.

References:
- https://github.com/pgvector/pgvector#hnsw
- https://aws.amazon.com/blogs/database/optimize-generative-ai-applications-with-pgvector-indexing/
- https://neon.com/docs/ai/ai-vector-search-optimization
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class DatasetScale(Enum):
    """Dataset scale categories for HNSW tuning."""

    SMALL = "small"  # <100K vectors
    MEDIUM = "medium"  # 100K - 1M vectors
    LARGE = "large"  # >1M vectors


@dataclass
class HNSWConfig:
    """HNSW index configuration parameters.

    Attributes:
        m: Maximum number of connections per node. Higher values improve
           recall but increase index size and build time. Default: 16.
        ef_construction: Size of dynamic candidate list during index build.
           Should be >= 2*m. Higher values improve index quality. Default: 64.
        ef_search: Size of dynamic candidate list during search. Higher
           values improve recall but slow queries. Default: 40.
        maintenance_work_mem: PostgreSQL memory for index builds. Default: "512MB".
        max_parallel_workers: Parallel workers for index build. Default: 2.
    """

    m: int = 16
    ef_construction: int = 64
    ef_search: int = 40
    maintenance_work_mem: str = "512MB"
    max_parallel_workers: int = 2

    # Presets for different scales (initialized after class definition)
    SMALL_SCALE: ClassVar[HNSWConfig]
    MEDIUM_SCALE: ClassVar[HNSWConfig]
    LARGE_SCALE: ClassVar[HNSWConfig]

    @classmethod
    def for_dataset_size(cls, vector_count: int) -> HNSWConfig:
        """Get optimal HNSW configuration based on dataset size.

        Args:
            vector_count: Number of vectors in the dataset.

        Returns:
            HNSWConfig with optimal parameters for the scale.

        Example:
            >>> config = HNSWConfig.for_dataset_size(500_000)
            >>> config.m
            24
            >>> config.ef_search
            100
        """
        if vector_count < 100_000:
            return cls.small_scale()
        elif vector_count < 1_000_000:
            return cls.medium_scale()
        else:
            return cls.large_scale()

    @classmethod
    def small_scale(cls) -> HNSWConfig:
        """Configuration for <100K vectors.

        Optimized for fast builds and low memory usage.
        Expected: ~0.95 recall, ~20 QPS.
        """
        return cls(
            m=16,
            ef_construction=64,
            ef_search=40,
            maintenance_work_mem="512MB",
            max_parallel_workers=2,
        )

    @classmethod
    def medium_scale(cls) -> HNSWConfig:
        """Configuration for 100K-1M vectors.

        Balanced for good recall and reasonable build times.
        Expected: ~0.99 recall, ~40 QPS.
        """
        return cls(
            m=24,
            ef_construction=128,
            ef_search=100,
            maintenance_work_mem="2GB",
            max_parallel_workers=7,
        )

    @classmethod
    def large_scale(cls) -> HNSWConfig:
        """Configuration for >1M vectors.

        Optimized for high recall at scale.
        Expected: ~0.998 recall, ~30 QPS.
        """
        return cls(
            m=32,
            ef_construction=200,
            ef_search=200,
            maintenance_work_mem="4GB",
            max_parallel_workers=7,
        )

    @classmethod
    def get_scale(cls, vector_count: int) -> DatasetScale:
        """Determine dataset scale category.

        Args:
            vector_count: Number of vectors.

        Returns:
            DatasetScale enum value.
        """
        if vector_count < 100_000:
            return DatasetScale.SMALL
        elif vector_count < 1_000_000:
            return DatasetScale.MEDIUM
        else:
            return DatasetScale.LARGE

    def get_create_index_sql(
        self,
        table: str = "document_chunks",
        column: str = "embedding",
        index_name: str = "idx_chunks_embedding_hnsw",
        operator_class: str = "halfvec_cosine_ops",
    ) -> str:
        """Generate CREATE INDEX SQL with current parameters.

        Args:
            table: Table name.
            column: Column name containing vectors.
            index_name: Name for the index.
            operator_class: pgvector operator class (halfvec_cosine_ops,
                vector_cosine_ops, halfvec_l2_ops, etc.).

        Returns:
            SQL CREATE INDEX statement.
        """
        return f"""CREATE INDEX IF NOT EXISTS {index_name}
ON {table}
USING hnsw ({column} {operator_class})
WITH (m = {self.m}, ef_construction = {self.ef_construction})"""

    def get_search_settings_sql(self) -> str:
        """Generate SQL to set search parameters.

        Returns:
            SQL SET statements for ef_search.
        """
        return f"SET LOCAL hnsw.ef_search = {self.ef_search}"

    def get_build_settings_sql(self) -> str:
        """Generate SQL to set index build parameters.

        Returns:
            SQL SET statements for maintenance_work_mem and parallel workers.
        """
        return f"""SET maintenance_work_mem = '{self.maintenance_work_mem}';
SET max_parallel_maintenance_workers = {self.max_parallel_workers}"""

    def apply_search_settings(self, session: Session) -> None:
        """Apply search settings to a SQLAlchemy session.

        Args:
            session: SQLAlchemy session.
        """
        from sqlalchemy import text

        session.execute(text(f"SET LOCAL hnsw.ef_search = {self.ef_search}"))

    def apply_build_settings(self, session: Session) -> None:
        """Apply index build settings to a SQLAlchemy session.

        Args:
            session: SQLAlchemy session.
        """
        from sqlalchemy import text

        session.execute(text(f"SET maintenance_work_mem = '{self.maintenance_work_mem}'"))
        session.execute(text(f"SET max_parallel_maintenance_workers = {self.max_parallel_workers}"))


def get_vector_count(session: Session, table: str = "document_chunks") -> int:
    """Get the count of vectors in a table.

    Args:
        session: SQLAlchemy session.
        table: Table name.

    Returns:
        Number of rows with non-null embeddings.
    """
    from sqlalchemy import text

    try:
        result = session.execute(text(f"SELECT COUNT(*) FROM {table} WHERE embedding IS NOT NULL"))
        return result.scalar() or 0
    except Exception:
        # embedding column doesn't exist yet
        return 0


def get_recommended_config(session: Session) -> HNSWConfig:
    """Get recommended HNSW config based on current dataset.

    Args:
        session: SQLAlchemy session.

    Returns:
        HNSWConfig with optimal parameters.
    """
    count = get_vector_count(session)
    return HNSWConfig.for_dataset_size(count)


# Initialize class-level presets
HNSWConfig.SMALL_SCALE = HNSWConfig.small_scale()
HNSWConfig.MEDIUM_SCALE = HNSWConfig.medium_scale()
HNSWConfig.LARGE_SCALE = HNSWConfig.large_scale()
