"""Add graph storage tables for knowledge graph (#1039)

Revision ID: add_graph_storage_tables
Revises: 6e9842c71775
Create Date: 2026-01-11

Adds tables for graph-based entity and relationship storage:
- entities: Canonical entity registry with embeddings for deduplication
- relationships: Directed edges between entities (adjacency list)
- entity_mentions: Provenance tracking linking entities to source documents

This enables:
- Cross-document entity linking
- N-hop graph traversal via recursive CTEs
- Entity resolution via embedding similarity
- Graph-enhanced retrieval (GraphRAG pattern)

Issue #1039: Graph storage layer for entities and relationships
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_graph_storage_tables"
down_revision: Union[str, Sequence[str], None] = "6e9842c71775"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create graph storage tables."""
    # =========================================================================
    # 1. Create entities table
    # =========================================================================
    op.create_table(
        "entities",
        # Primary key
        sa.Column("entity_id", sa.String(36), primary_key=True),
        # Tenant isolation (defense-in-depth)
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        # Entity identification
        sa.Column("canonical_name", sa.String(512), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=True),
        # Embedding for semantic entity matching/deduplication
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("embedding_model", sa.String(100), nullable=True),
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
        # Entity resolution tracking
        sa.Column("aliases", sa.Text(), nullable=True),  # JSON array
        sa.Column("merge_count", sa.Integer(), nullable=False, server_default="1"),
        # Metadata
        sa.Column("metadata_json", sa.Text(), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Unique constraint on (tenant_id, canonical_name)
        sa.UniqueConstraint("tenant_id", "canonical_name", name="uq_entity_tenant_name"),
    )

    # Create indexes for entities table
    op.create_index("idx_entities_tenant", "entities", ["tenant_id"])
    op.create_index("idx_entities_type", "entities", ["entity_type"])
    op.create_index("idx_entities_tenant_type", "entities", ["tenant_id", "entity_type"])
    op.create_index("idx_entities_canonical_name", "entities", ["canonical_name"])

    # =========================================================================
    # 2. Create relationships table
    # =========================================================================
    op.create_table(
        "relationships",
        # Primary key
        sa.Column("relationship_id", sa.String(36), primary_key=True),
        # Tenant isolation
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="default"),
        # Source and target entities (foreign keys)
        sa.Column(
            "source_entity_id",
            sa.String(36),
            sa.ForeignKey("entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            sa.String(36),
            sa.ForeignKey("entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Relationship metadata
        sa.Column("relationship_type", sa.String(64), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        # Additional metadata
        sa.Column("metadata_json", sa.Text(), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Unique constraint: one relationship type per source-target pair per tenant
        sa.UniqueConstraint(
            "tenant_id",
            "source_entity_id",
            "target_entity_id",
            "relationship_type",
            name="uq_relationship_tuple",
        ),
    )

    # Create indexes for relationships table (critical for N-hop traversal)
    op.create_index("idx_relationships_source", "relationships", ["source_entity_id"])
    op.create_index("idx_relationships_target", "relationships", ["target_entity_id"])
    op.create_index("idx_relationships_type", "relationships", ["relationship_type"])
    op.create_index(
        "idx_relationships_source_type",
        "relationships",
        ["source_entity_id", "relationship_type"],
    )
    op.create_index(
        "idx_relationships_target_type",
        "relationships",
        ["target_entity_id", "relationship_type"],
    )
    op.create_index("idx_relationships_tenant", "relationships", ["tenant_id"])
    op.create_index("idx_relationships_confidence", "relationships", ["confidence"])

    # =========================================================================
    # 3. Create entity_mentions table (provenance tracking)
    # =========================================================================
    op.create_table(
        "entity_mentions",
        # Primary key
        sa.Column("mention_id", sa.String(36), primary_key=True),
        # Foreign key to entity
        sa.Column(
            "entity_id",
            sa.String(36),
            sa.ForeignKey("entities.entity_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Source references (at least one should be set)
        sa.Column(
            "chunk_id",
            sa.String(36),
            sa.ForeignKey("document_chunks.chunk_id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "memory_id",
            sa.String(36),
            sa.ForeignKey("memories.memory_id", ondelete="CASCADE"),
            nullable=True,
        ),
        # Mention details
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("mention_text", sa.String(512), nullable=True),
        # Position in source
        sa.Column("char_offset_start", sa.Integer(), nullable=True),
        sa.Column("char_offset_end", sa.Integer(), nullable=True),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    # Create indexes for entity_mentions table
    op.create_index("idx_entity_mentions_entity", "entity_mentions", ["entity_id"])
    op.create_index("idx_entity_mentions_chunk", "entity_mentions", ["chunk_id"])
    op.create_index("idx_entity_mentions_memory", "entity_mentions", ["memory_id"])
    op.create_index("idx_entity_mentions_confidence", "entity_mentions", ["confidence"])


def downgrade() -> None:
    """Drop graph storage tables."""
    # Drop entity_mentions table and indexes
    op.drop_index("idx_entity_mentions_confidence", table_name="entity_mentions")
    op.drop_index("idx_entity_mentions_memory", table_name="entity_mentions")
    op.drop_index("idx_entity_mentions_chunk", table_name="entity_mentions")
    op.drop_index("idx_entity_mentions_entity", table_name="entity_mentions")
    op.drop_table("entity_mentions")

    # Drop relationships table and indexes
    op.drop_index("idx_relationships_confidence", table_name="relationships")
    op.drop_index("idx_relationships_tenant", table_name="relationships")
    op.drop_index("idx_relationships_target_type", table_name="relationships")
    op.drop_index("idx_relationships_source_type", table_name="relationships")
    op.drop_index("idx_relationships_type", table_name="relationships")
    op.drop_index("idx_relationships_target", table_name="relationships")
    op.drop_index("idx_relationships_source", table_name="relationships")
    op.drop_table("relationships")

    # Drop entities table and indexes
    op.drop_index("idx_entities_canonical_name", table_name="entities")
    op.drop_index("idx_entities_tenant_type", table_name="entities")
    op.drop_index("idx_entities_type", table_name="entities")
    op.drop_index("idx_entities_tenant", table_name="entities")
    op.drop_table("entities")
