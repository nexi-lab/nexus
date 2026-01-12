#!/usr/bin/env python3
"""End-to-end test script for Graph Storage Layer (Issue #1039).

Tests the GraphStore against a real PostgreSQL database to verify:
- Entity CRUD operations
- Relationship CRUD operations
- N-hop neighbor traversal (recursive CTEs)
- Subgraph extraction
- Entity mentions (provenance tracking)

Usage:
    # Using NEXUS_DATABASE_URL environment variable
    export NEXUS_DATABASE_URL=postgresql://user:pass@localhost/nexus
    python scripts/test_graph_store_e2e.py

    # Or pass database URL directly
    python scripts/test_graph_store_e2e.py --database-url postgresql://user:pass@localhost/nexus

    # With verbose logging
    python scripts/test_graph_store_e2e.py -v

References:
    - https://github.com/nexi-lab/nexus/issues/1039
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.search.graph_store import GraphStore
from nexus.storage.models import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class GraphStoreE2ETest:
    """End-to-end test runner for GraphStore."""

    def __init__(self, database_url: str, tenant_id: str = "e2e-test"):
        self.database_url = database_url
        self.tenant_id = tenant_id
        self.engine = None
        self.session = None
        self.store = None
        self.passed = 0
        self.failed = 0

    async def setup(self):
        """Set up database connection and create tables."""
        # Convert to async URL
        async_url = self.database_url
        if async_url.startswith("postgresql://"):
            async_url = async_url.replace("postgresql://", "postgresql+asyncpg://")

        logger.info("Connecting to database...")
        self.engine = create_async_engine(async_url, echo=False)

        # Create tables if they don't exist
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ready")

        # Create session
        async_session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.session = async_session_factory()
        self.store = GraphStore(self.session, tenant_id=self.tenant_id)
        logger.info(f"GraphStore initialized with tenant_id={self.tenant_id}")

    async def teardown(self):
        """Clean up test data and close connections."""
        if self.session:
            # Clean up test data
            logger.info("Cleaning up test data...")
            await self.session.execute(
                text(
                    "DELETE FROM entity_mentions WHERE entity_id IN (SELECT entity_id FROM entities WHERE tenant_id = :tenant)"
                ),
                {"tenant": self.tenant_id},
            )
            await self.session.execute(
                text("DELETE FROM relationships WHERE tenant_id = :tenant"),
                {"tenant": self.tenant_id},
            )
            await self.session.execute(
                text("DELETE FROM entities WHERE tenant_id = :tenant"),
                {"tenant": self.tenant_id},
            )
            await self.session.commit()
            await self.session.close()
            logger.info("Test data cleaned up")

        if self.engine:
            await self.engine.dispose()

    def log_result(self, test_name: str, passed: bool, message: str = ""):
        """Log test result."""
        if passed:
            self.passed += 1
            logger.info(f"  [PASS] {test_name}" + (f" - {message}" if message else ""))
        else:
            self.failed += 1
            logger.error(f"  [FAIL] {test_name}" + (f" - {message}" if message else ""))

    async def test_entity_crud(self):
        """Test entity create, read, update, delete."""
        logger.info("\n" + "=" * 60)
        logger.info("TEST: Entity CRUD Operations")
        logger.info("=" * 60)

        # Create
        start = time.time()
        entity_id, was_created = await self.store.add_entity(
            name="Alice Johnson",
            entity_type="PERSON",
            metadata={"role": "engineer", "department": "AI"},
        )
        await self.session.commit()
        create_time = (time.time() - start) * 1000

        self.log_result(
            "Create entity",
            was_created and entity_id is not None,
            f"id={entity_id[:8]}..., time={create_time:.2f}ms",
        )

        # Read
        start = time.time()
        entity = await self.store.get_entity(entity_id)
        read_time = (time.time() - start) * 1000

        self.log_result(
            "Read entity",
            entity is not None and entity.canonical_name == "Alice Johnson",
            f"name={entity.canonical_name}, type={entity.entity_type}, time={read_time:.2f}ms",
        )

        # Update
        start = time.time()
        success = await self.store.update_entity(
            entity_id,
            metadata={"role": "senior engineer", "level": "L5"},
        )
        await self.session.commit()
        update_time = (time.time() - start) * 1000

        entity = await self.store.get_entity(entity_id)
        self.log_result(
            "Update entity",
            success and entity.metadata.get("role") == "senior engineer",
            f"metadata={entity.metadata}, time={update_time:.2f}ms",
        )

        # Delete
        start = time.time()
        success = await self.store.delete_entity(entity_id)
        await self.session.commit()
        delete_time = (time.time() - start) * 1000

        entity = await self.store.get_entity(entity_id)
        self.log_result(
            "Delete entity",
            success and entity is None,
            f"time={delete_time:.2f}ms",
        )

    async def test_entity_deduplication(self):
        """Test entity deduplication by name."""
        logger.info("\n" + "=" * 60)
        logger.info("TEST: Entity Deduplication")
        logger.info("=" * 60)

        # Create first entity
        entity_id1, was_created1 = await self.store.add_entity(
            name="Acme Corporation",
            entity_type="ORG",
        )
        await self.session.commit()

        self.log_result(
            "Create first entity",
            was_created1,
            f"id={entity_id1[:8]}...",
        )

        # Try to create duplicate
        entity_id2, was_created2 = await self.store.add_entity(
            name="Acme Corporation",
            entity_type="ORG",
        )

        self.log_result(
            "Detect duplicate",
            not was_created2 and entity_id2 == entity_id1,
            f"returned existing id={entity_id2[:8]}...",
        )

        # Clean up
        await self.store.delete_entity(entity_id1)
        await self.session.commit()

    async def test_relationships(self):
        """Test relationship creation and querying."""
        logger.info("\n" + "=" * 60)
        logger.info("TEST: Relationship Operations")
        logger.info("=" * 60)

        # Create entities
        alice_id, _ = await self.store.add_entity(name="Alice", entity_type="PERSON")
        bob_id, _ = await self.store.add_entity(name="Bob", entity_type="PERSON")
        team_id, _ = await self.store.add_entity(name="AI Team", entity_type="ORG")
        await self.session.commit()

        logger.info(
            f"  Created entities: Alice={alice_id[:8]}, Bob={bob_id[:8]}, Team={team_id[:8]}"
        )

        # Create relationships
        start = time.time()
        rel1_id, was_created1 = await self.store.add_relationship(
            alice_id, bob_id, "WORKS_WITH", confidence=0.95
        )
        rel2_id, was_created2 = await self.store.add_relationship(
            alice_id, team_id, "PART_OF", confidence=0.9
        )
        await self.session.commit()
        create_time = (time.time() - start) * 1000

        self.log_result(
            "Create relationships",
            was_created1 and was_created2,
            f"2 relationships created in {create_time:.2f}ms",
        )

        # Query outgoing
        start = time.time()
        rels = await self.store.get_relationships(alice_id, direction="outgoing")
        query_time = (time.time() - start) * 1000

        self.log_result(
            "Query outgoing relationships",
            len(rels) == 2,
            f"found {len(rels)} relationships in {query_time:.2f}ms",
        )

        # Query with type filter
        filtered = await self.store.get_relationships(
            alice_id, direction="outgoing", rel_types=["WORKS_WITH"]
        )
        self.log_result(
            "Filter by relationship type",
            len(filtered) == 1 and filtered[0].relationship_type == "WORKS_WITH",
            f"filtered to {len(filtered)} relationship",
        )

        # Clean up
        await self.store.delete_entity(alice_id)
        await self.store.delete_entity(bob_id)
        await self.store.delete_entity(team_id)
        await self.session.commit()

    async def test_n_hop_traversal(self):
        """Test N-hop neighbor traversal using recursive CTE."""
        logger.info("\n" + "=" * 60)
        logger.info("TEST: N-Hop Graph Traversal (Recursive CTE)")
        logger.info("=" * 60)

        # Create linear graph: A -> B -> C -> D -> E
        entity_ids = []
        names = ["Node_A", "Node_B", "Node_C", "Node_D", "Node_E"]

        for name in names:
            eid, _ = await self.store.add_entity(name=name, entity_type="CONCEPT")
            entity_ids.append(eid)
        await self.session.commit()

        # Create chain
        for i in range(len(entity_ids) - 1):
            await self.store.add_relationship(entity_ids[i], entity_ids[i + 1], "CONNECTS_TO")
        await self.session.commit()

        logger.info("  Created chain: A -> B -> C -> D -> E")

        # Test 1-hop
        start = time.time()
        neighbors_1 = await self.store.get_neighbors(entity_ids[0], hops=1)
        time_1 = (time.time() - start) * 1000
        names_1 = {n.entity.canonical_name for n in neighbors_1}

        self.log_result(
            "1-hop traversal",
            names_1 == {"Node_B"},
            f"found {names_1}, time={time_1:.2f}ms",
        )

        # Test 2-hop
        start = time.time()
        neighbors_2 = await self.store.get_neighbors(entity_ids[0], hops=2)
        time_2 = (time.time() - start) * 1000
        names_2 = {n.entity.canonical_name for n in neighbors_2}

        self.log_result(
            "2-hop traversal",
            names_2 == {"Node_B", "Node_C"},
            f"found {names_2}, time={time_2:.2f}ms",
        )

        # Test 3-hop
        start = time.time()
        neighbors_3 = await self.store.get_neighbors(entity_ids[0], hops=3)
        time_3 = (time.time() - start) * 1000
        names_3 = {n.entity.canonical_name for n in neighbors_3}

        self.log_result(
            "3-hop traversal",
            names_3 == {"Node_B", "Node_C", "Node_D"},
            f"found {names_3}, time={time_3:.2f}ms",
        )

        # Test 4-hop (reaches all)
        start = time.time()
        neighbors_4 = await self.store.get_neighbors(entity_ids[0], hops=4)
        time_4 = (time.time() - start) * 1000
        names_4 = {n.entity.canonical_name for n in neighbors_4}

        self.log_result(
            "4-hop traversal",
            names_4 == {"Node_B", "Node_C", "Node_D", "Node_E"},
            f"found {names_4}, time={time_4:.2f}ms",
        )

        # Verify depth tracking
        logger.info("  Depth tracking:")
        for n in sorted(neighbors_4, key=lambda x: x.depth):
            logger.info(f"    {n.entity.canonical_name}: depth={n.depth}, path_len={len(n.path)}")

        # Clean up
        for eid in entity_ids:
            await self.store.delete_entity(eid)
        await self.session.commit()

    async def test_subgraph_extraction(self):
        """Test subgraph extraction for GraphRAG."""
        logger.info("\n" + "=" * 60)
        logger.info("TEST: Subgraph Extraction (GraphRAG Context)")
        logger.info("=" * 60)

        # Create org chart
        ceo_id, _ = await self.store.add_entity(name="CEO Jane", entity_type="PERSON")
        cto_id, _ = await self.store.add_entity(name="CTO Mike", entity_type="PERSON")
        dev1_id, _ = await self.store.add_entity(name="Dev Alice", entity_type="PERSON")
        dev2_id, _ = await self.store.add_entity(name="Dev Bob", entity_type="PERSON")
        company_id, _ = await self.store.add_entity(name="TechCorp", entity_type="ORG")

        await self.store.add_relationship(ceo_id, company_id, "LEADS")
        await self.store.add_relationship(cto_id, ceo_id, "REPORTS_TO")
        await self.store.add_relationship(dev1_id, cto_id, "REPORTS_TO")
        await self.store.add_relationship(dev2_id, cto_id, "REPORTS_TO")
        await self.store.add_relationship(dev1_id, dev2_id, "WORKS_WITH")
        await self.session.commit()

        logger.info("  Created org chart: CEO <- CTO <- [Dev1, Dev2]")

        # Extract subgraph from CTO
        start = time.time()
        subgraph = await self.store.get_subgraph([cto_id], max_hops=2)
        extract_time = (time.time() - start) * 1000

        entity_names = {e.canonical_name for e in subgraph.entities}
        expected = {"CTO Mike", "CEO Jane", "Dev Alice", "Dev Bob", "TechCorp"}

        self.log_result(
            "Extract subgraph",
            entity_names == expected,
            f"{len(subgraph.entities)} entities, {len(subgraph.relationships)} relationships in {extract_time:.2f}ms",
        )

        # Test JSON serialization
        graph_dict = subgraph.to_dict()
        json_str = json.dumps(graph_dict, indent=2)
        logger.info(f"  Subgraph JSON ({len(json_str)} bytes):")
        for line in json_str.split("\n")[:15]:
            logger.info(f"    {line}")
        if len(json_str.split("\n")) > 15:
            logger.info("    ...")

        # Clean up
        for eid in [ceo_id, cto_id, dev1_id, dev2_id, company_id]:
            await self.store.delete_entity(eid)
        await self.session.commit()

    async def test_bulk_operations(self):
        """Test bulk entity and relationship creation."""
        logger.info("\n" + "=" * 60)
        logger.info("TEST: Bulk Operations")
        logger.info("=" * 60)

        # Bulk create entities
        entities_data = [{"name": f"BulkEntity_{i}", "entity_type": "CONCEPT"} for i in range(10)]

        start = time.time()
        results = await self.store.add_entities_bulk(entities_data)
        await self.session.commit()
        bulk_time = (time.time() - start) * 1000

        created_count = sum(1 for _, was_created in results if was_created)
        entity_ids = [eid for eid, _ in results]

        self.log_result(
            "Bulk create 10 entities",
            created_count == 10,
            f"created {created_count} in {bulk_time:.2f}ms ({bulk_time / 10:.2f}ms/entity)",
        )

        # Bulk create relationships (chain)
        rel_data = [
            {
                "source_entity_id": entity_ids[i],
                "target_entity_id": entity_ids[i + 1],
                "relationship_type": "RELATES_TO",
            }
            for i in range(len(entity_ids) - 1)
        ]

        start = time.time()
        rel_results = await self.store.add_relationships_bulk(rel_data)
        await self.session.commit()
        rel_time = (time.time() - start) * 1000

        rel_created = sum(1 for _, was_created in rel_results if was_created)

        self.log_result(
            "Bulk create 9 relationships",
            rel_created == 9,
            f"created {rel_created} in {rel_time:.2f}ms ({rel_time / 9:.2f}ms/relationship)",
        )

        # Clean up
        for eid in entity_ids:
            await self.store.delete_entity(eid)
        await self.session.commit()

    async def run_all_tests(self):
        """Run all tests."""
        logger.info("\n")
        logger.info("=" * 60)
        logger.info("  Graph Storage Layer E2E Test (Issue #1039)")
        logger.info("=" * 60)
        logger.info(f"Database: {self.database_url[:50]}...")
        logger.info(f"Tenant ID: {self.tenant_id}")
        logger.info(f"Timestamp: {datetime.now(UTC).isoformat()}")

        await self.test_entity_crud()
        await self.test_entity_deduplication()
        await self.test_relationships()
        await self.test_n_hop_traversal()
        await self.test_subgraph_extraction()
        await self.test_bulk_operations()

        # Summary
        logger.info("\n" + "=" * 60)
        logger.info("  SUMMARY")
        logger.info("=" * 60)
        total = self.passed + self.failed
        logger.info(f"  Total:  {total} tests")
        logger.info(f"  Passed: {self.passed} tests")
        logger.info(f"  Failed: {self.failed} tests")

        if self.failed == 0:
            logger.info("\n  ALL TESTS PASSED!")
            return True
        else:
            logger.error(f"\n  {self.failed} TESTS FAILED!")
            return False


async def main():
    parser = argparse.ArgumentParser(description="Graph Storage E2E Test")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("NEXUS_DATABASE_URL"),
        help="PostgreSQL database URL (default: NEXUS_DATABASE_URL env var)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if not args.database_url:
        logger.error("No database URL provided!")
        logger.error("Set NEXUS_DATABASE_URL environment variable or use --database-url")
        sys.exit(1)

    # Verify it's PostgreSQL
    if not args.database_url.startswith("postgresql"):
        logger.error("This test requires PostgreSQL!")
        logger.error(f"Got: {args.database_url}")
        sys.exit(1)

    test = GraphStoreE2ETest(args.database_url)

    try:
        await test.setup()
        success = await test.run_all_tests()
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Test failed with error: {e}")
        sys.exit(1)
    finally:
        await test.teardown()


if __name__ == "__main__":
    asyncio.run(main())
