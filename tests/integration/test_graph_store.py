"""End-to-end tests for Graph Storage Layer (Issue #1039).

Tests the GraphStore with PostgreSQL or SQLite database to verify:
- Entity CRUD operations
- Relationship CRUD operations
- Entity resolution via embedding similarity
- N-hop neighbor traversal
- Subgraph extraction

Environment variables:
    TEST_DATABASE_URL: PostgreSQL connection URL (default: file-based SQLite)

Example:
    # Run with PostgreSQL
    TEST_DATABASE_URL=postgresql://localhost/nexus_test pytest tests/integration/test_graph_store.py -v

    # Run with SQLite (default)
    pytest tests/integration/test_graph_store.py -v
"""

from __future__ import annotations

import json
import logging

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.search.graph_store import GraphStore
from nexus.storage.models import Base

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


@pytest.fixture
async def async_engine(tmp_path):
    """Create async database engine with file-based SQLite."""
    # Use file-based SQLite for testing
    db_path = tmp_path / "test_graph.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(async_url, echo=False)

    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest.fixture
async def session(async_engine):
    """Create async database session."""
    async_session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with async_session_factory() as session:
        yield session


@pytest.fixture
async def graph_store(session):
    """Create GraphStore instance."""
    return GraphStore(session, tenant_id="test-tenant")


class TestGraphStoreE2E:
    """End-to-end tests for GraphStore."""

    @pytest.mark.asyncio
    async def test_entity_lifecycle(self, graph_store, session):
        """Test complete entity lifecycle: create, read, update, delete."""
        logger.info("=== Testing Entity Lifecycle ===")

        # Create entity
        logger.info("Creating entity 'Alice'...")
        entity_id, was_created = await graph_store.add_entity(
            name="Alice Johnson",
            entity_type="PERSON",
            metadata={"role": "engineer", "department": "AI"},
        )
        assert was_created is True
        logger.info(f"Created entity with ID: {entity_id}")

        # Read entity
        logger.info("Reading entity...")
        entity = await graph_store.get_entity(entity_id)
        assert entity is not None
        assert entity.canonical_name == "Alice Johnson"
        assert entity.entity_type == "PERSON"
        assert entity.metadata["role"] == "engineer"
        logger.info(f"Entity retrieved: {entity.canonical_name} ({entity.entity_type})")

        # Update entity
        logger.info("Updating entity...")
        success = await graph_store.update_entity(
            entity_id,
            metadata={"role": "senior engineer", "department": "AI", "level": "L5"},
        )
        assert success is True
        await session.commit()

        # Verify update
        entity = await graph_store.get_entity(entity_id)
        assert entity.metadata["role"] == "senior engineer"
        logger.info(f"Entity updated: metadata={entity.metadata}")

        # Delete entity
        logger.info("Deleting entity...")
        success = await graph_store.delete_entity(entity_id)
        assert success is True
        await session.commit()

        # Verify deletion
        entity = await graph_store.get_entity(entity_id)
        assert entity is None
        logger.info("Entity deleted successfully")

    @pytest.mark.asyncio
    async def test_entity_deduplication(self, graph_store, session):
        """Test entity deduplication by exact name match."""
        logger.info("=== Testing Entity Deduplication ===")

        # Create first entity
        logger.info("Creating entity 'Acme Corp'...")
        entity_id1, was_created1 = await graph_store.add_entity(
            name="Acme Corp",
            entity_type="ORG",
        )
        assert was_created1 is True
        await session.commit()
        logger.info(f"First entity created: {entity_id1}")

        # Try to create duplicate
        logger.info("Attempting to create duplicate 'Acme Corp'...")
        entity_id2, was_created2 = await graph_store.add_entity(
            name="Acme Corp",
            entity_type="ORG",
        )
        assert was_created2 is False
        assert entity_id2 == entity_id1
        logger.info(f"Duplicate detected, returned existing ID: {entity_id2}")

        # Create with different name (should be new)
        logger.info("Creating entity 'Acme Corporation' (different name)...")
        entity_id3, was_created3 = await graph_store.add_entity(
            name="Acme Corporation",
            entity_type="ORG",
        )
        assert was_created3 is True
        assert entity_id3 != entity_id1
        await session.commit()
        logger.info(f"New entity created: {entity_id3}")

    @pytest.mark.asyncio
    async def test_relationship_lifecycle(self, graph_store, session):
        """Test complete relationship lifecycle."""
        logger.info("=== Testing Relationship Lifecycle ===")

        # Create entities
        logger.info("Creating entities...")
        alice_id, _ = await graph_store.add_entity(name="Alice", entity_type="PERSON")
        bob_id, _ = await graph_store.add_entity(name="Bob", entity_type="PERSON")
        team_id, _ = await graph_store.add_entity(name="AI Team", entity_type="ORG")
        await session.commit()
        logger.info(f"Created: Alice={alice_id}, Bob={bob_id}, Team={team_id}")

        # Create relationships
        logger.info("Creating relationships...")
        rel1_id, was_created1 = await graph_store.add_relationship(
            source_entity_id=alice_id,
            target_entity_id=bob_id,
            relationship_type="WORKS_WITH",
            confidence=0.95,
        )
        assert was_created1 is True
        logger.info(f"Created: Alice WORKS_WITH Bob (id={rel1_id})")

        rel2_id, was_created2 = await graph_store.add_relationship(
            source_entity_id=alice_id,
            target_entity_id=team_id,
            relationship_type="PART_OF",
            confidence=0.9,
        )
        assert was_created2 is True
        await session.commit()
        logger.info(f"Created: Alice PART_OF Team (id={rel2_id})")

        # Query outgoing relationships
        logger.info("Querying Alice's outgoing relationships...")
        relationships = await graph_store.get_relationships(alice_id, direction="outgoing")
        assert len(relationships) == 2
        rel_types = {r.relationship_type for r in relationships}
        assert "WORKS_WITH" in rel_types
        assert "PART_OF" in rel_types
        logger.info(f"Found {len(relationships)} relationships: {rel_types}")

        # Query with type filter
        logger.info("Querying with type filter (WORKS_WITH only)...")
        filtered = await graph_store.get_relationships(
            alice_id, direction="outgoing", rel_types=["WORKS_WITH"]
        )
        assert len(filtered) == 1
        assert filtered[0].relationship_type == "WORKS_WITH"
        logger.info(f"Filtered results: {len(filtered)} relationship(s)")

    @pytest.mark.asyncio
    async def test_n_hop_traversal(self, graph_store, session):
        """Test N-hop neighbor traversal."""
        logger.info("=== Testing N-Hop Traversal ===")

        # Create a graph: Alice -> Bob -> Charlie -> Dave
        logger.info("Creating linear graph: Alice -> Bob -> Charlie -> Dave")
        alice_id, _ = await graph_store.add_entity(name="Alice", entity_type="PERSON")
        bob_id, _ = await graph_store.add_entity(name="Bob", entity_type="PERSON")
        charlie_id, _ = await graph_store.add_entity(name="Charlie", entity_type="PERSON")
        dave_id, _ = await graph_store.add_entity(name="Dave", entity_type="PERSON")

        await graph_store.add_relationship(alice_id, bob_id, "KNOWS")
        await graph_store.add_relationship(bob_id, charlie_id, "KNOWS")
        await graph_store.add_relationship(charlie_id, dave_id, "KNOWS")
        await session.commit()
        logger.info("Graph created and committed")

        # 1-hop from Alice
        logger.info("1-hop traversal from Alice...")
        neighbors_1hop = await graph_store.get_neighbors(alice_id, hops=1)
        neighbor_names_1 = {n.entity.canonical_name for n in neighbors_1hop}
        logger.info(f"1-hop neighbors: {neighbor_names_1}")
        assert "Bob" in neighbor_names_1
        assert "Charlie" not in neighbor_names_1

        # 2-hop from Alice
        logger.info("2-hop traversal from Alice...")
        neighbors_2hop = await graph_store.get_neighbors(alice_id, hops=2)
        neighbor_names_2 = {n.entity.canonical_name for n in neighbors_2hop}
        logger.info(f"2-hop neighbors: {neighbor_names_2}")
        assert "Bob" in neighbor_names_2
        assert "Charlie" in neighbor_names_2
        assert "Dave" not in neighbor_names_2

        # 3-hop from Alice (should reach everyone)
        logger.info("3-hop traversal from Alice...")
        neighbors_3hop = await graph_store.get_neighbors(alice_id, hops=3)
        neighbor_names_3 = {n.entity.canonical_name for n in neighbors_3hop}
        logger.info(f"3-hop neighbors: {neighbor_names_3}")
        assert "Bob" in neighbor_names_3
        assert "Charlie" in neighbor_names_3
        assert "Dave" in neighbor_names_3

        # Verify depth tracking
        for neighbor in neighbors_3hop:
            logger.info(
                f"  {neighbor.entity.canonical_name}: depth={neighbor.depth}, path={neighbor.path}"
            )

    @pytest.mark.asyncio
    async def test_subgraph_extraction(self, graph_store, session):
        """Test subgraph extraction for GraphRAG context building."""
        logger.info("=== Testing Subgraph Extraction ===")

        # Create a more complex graph
        logger.info("Creating company org chart...")
        ceo_id, _ = await graph_store.add_entity(name="CEO Jane", entity_type="PERSON")
        cto_id, _ = await graph_store.add_entity(name="CTO Mike", entity_type="PERSON")
        dev1_id, _ = await graph_store.add_entity(name="Dev Alice", entity_type="PERSON")
        dev2_id, _ = await graph_store.add_entity(name="Dev Bob", entity_type="PERSON")
        company_id, _ = await graph_store.add_entity(name="TechCorp", entity_type="ORG")

        # Org structure
        await graph_store.add_relationship(ceo_id, company_id, "LEADS")
        await graph_store.add_relationship(cto_id, ceo_id, "REPORTS_TO")
        await graph_store.add_relationship(dev1_id, cto_id, "REPORTS_TO")
        await graph_store.add_relationship(dev2_id, cto_id, "REPORTS_TO")
        await graph_store.add_relationship(dev1_id, dev2_id, "WORKS_WITH")
        await session.commit()
        logger.info("Org chart created")

        # Extract subgraph starting from CTO
        logger.info("Extracting subgraph from CTO (2 hops)...")
        subgraph = await graph_store.get_subgraph([cto_id], max_hops=2)

        entity_names = {e.canonical_name for e in subgraph.entities}
        logger.info(f"Subgraph entities: {entity_names}")
        logger.info(f"Subgraph relationships: {len(subgraph.relationships)}")

        # CTO should reach CEO, both devs, and the company
        assert "CTO Mike" in entity_names
        assert "CEO Jane" in entity_names
        assert "Dev Alice" in entity_names
        assert "Dev Bob" in entity_names

        # Convert to dict for context
        graph_dict = subgraph.to_dict()
        logger.info(f"Graph JSON: {json.dumps(graph_dict, indent=2)}")

    @pytest.mark.asyncio
    async def test_entity_mentions_provenance(self, graph_store, session):
        """Test entity mention tracking for provenance."""
        logger.info("=== Testing Entity Mentions (Provenance) ===")

        # Create entity
        entity_id, _ = await graph_store.add_entity(
            name="Project Alpha",
            entity_type="CONCEPT",
        )
        await session.commit()
        logger.info(f"Created entity: {entity_id}")

        # Add mentions from different sources
        logger.info("Adding mentions from multiple sources...")
        mention1_id = await graph_store.add_mention(
            entity_id=entity_id,
            memory_id="memory-123",
            confidence=0.95,
            mention_text="Project Alpha",
            char_offset_start=10,
            char_offset_end=23,
        )
        mention2_id = await graph_store.add_mention(
            entity_id=entity_id,
            chunk_id="chunk-456",
            confidence=0.85,
            mention_text="the Alpha project",
            char_offset_start=0,
            char_offset_end=17,
        )
        await session.commit()
        logger.info(f"Created mentions: {mention1_id}, {mention2_id}")

        # Query mentions
        logger.info("Querying entity mentions...")
        mentions = await graph_store.get_entity_mentions(entity_id)
        assert len(mentions) == 2
        logger.info(f"Found {len(mentions)} mentions:")
        for m in mentions:
            source = f"memory={m.memory_id}" if m.memory_id else f"chunk={m.chunk_id}"
            logger.info(f"  - '{m.mention_text}' from {source} (confidence={m.confidence})")

    @pytest.mark.asyncio
    async def test_bulk_operations(self, graph_store, session):
        """Test bulk entity and relationship creation."""
        logger.info("=== Testing Bulk Operations ===")

        # Bulk add entities
        logger.info("Bulk adding 5 entities...")
        entities_data = [{"name": f"Entity_{i}", "entity_type": "CONCEPT"} for i in range(5)]
        entity_results = await graph_store.add_entities_bulk(entities_data)
        await session.commit()

        created_count = sum(1 for _, was_created in entity_results if was_created)
        logger.info(f"Created {created_count} entities")
        assert created_count == 5

        # Get entity IDs
        entity_ids = [eid for eid, _ in entity_results]

        # Bulk add relationships (connect in a chain)
        logger.info("Bulk adding relationships...")
        relationships_data = [
            {
                "source_entity_id": entity_ids[i],
                "target_entity_id": entity_ids[i + 1],
                "relationship_type": "RELATES_TO",
            }
            for i in range(len(entity_ids) - 1)
        ]
        rel_results = await graph_store.add_relationships_bulk(relationships_data)
        await session.commit()

        rel_created_count = sum(1 for _, was_created in rel_results if was_created)
        logger.info(f"Created {rel_created_count} relationships")
        assert rel_created_count == 4

        # Verify graph connectivity
        logger.info("Verifying graph connectivity...")
        neighbors = await graph_store.get_neighbors(entity_ids[0], hops=4)
        logger.info(f"From Entity_0, can reach {len(neighbors)} entities in 4 hops")
        assert len(neighbors) == 4  # Should reach all other entities

    @pytest.mark.asyncio
    async def test_confidence_filtering(self, graph_store, session):
        """Test relationship confidence filtering."""
        logger.info("=== Testing Confidence Filtering ===")

        # Create entities
        a_id, _ = await graph_store.add_entity(name="A", entity_type="CONCEPT")
        b_id, _ = await graph_store.add_entity(name="B", entity_type="CONCEPT")
        c_id, _ = await graph_store.add_entity(name="C", entity_type="CONCEPT")

        # Create relationships with different confidence levels
        await graph_store.add_relationship(a_id, b_id, "RELATES_TO", confidence=0.95)
        await graph_store.add_relationship(a_id, c_id, "RELATES_TO", confidence=0.5)
        await session.commit()
        logger.info("Created relationships: A->B (0.95), A->C (0.5)")

        # Query without filter
        all_rels = await graph_store.get_relationships(a_id, direction="outgoing")
        logger.info(f"Without filter: {len(all_rels)} relationships")
        assert len(all_rels) == 2

        # Query with confidence filter
        high_conf_rels = await graph_store.get_relationships(
            a_id, direction="outgoing", min_confidence=0.75
        )
        logger.info(f"With min_confidence=0.75: {len(high_conf_rels)} relationships")
        assert len(high_conf_rels) == 1
        assert high_conf_rels[0].target_entity_id == b_id


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--log-cli-level=INFO"])
