#!/usr/bin/env python3
"""End-to-end test script for Graph-Enhanced Retrieval (Issue #1040).

Tests the GraphEnhancedRetriever against real database to verify:
- Dual-level search (low-level entity + high-level theme)
- Graph-enhanced fusion scoring
- Entity expansion and context enrichment
- Integration with SemanticSearch and GraphStore

Usage:
    # Using NEXUS_DATABASE_URL environment variable
    export NEXUS_DATABASE_URL=postgresql://user:pass@localhost/nexus
    python scripts/test_graph_retrieval_e2e.py

    # Or use SQLite for quick testing
    python scripts/test_graph_retrieval_e2e.py --database-url sqlite:///test_graph_retrieval.db

    # With verbose logging
    python scripts/test_graph_retrieval_e2e.py -v

References:
    - https://github.com/nexi-lab/nexus/issues/1040
    - LightRAG Paper: https://arxiv.org/abs/2410.05779
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nexus.search.graph_retrieval import (
    GraphEnhancedRetriever,
    GraphEnhancedSearchResult,
    GraphRetrievalConfig,
    graph_enhanced_fusion,
)
from nexus.search.graph_store import GraphStore
from nexus.search.semantic import SemanticSearchResult
from nexus.storage.models import Base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Test documents simulating a codebase
TEST_DOCUMENTS = {
    "/docs/auth.md": """# Authentication System

The authentication system uses JWT (JSON Web Tokens) for secure user authentication.
Users can authenticate using OAuth2 or username/password credentials.

## Components
- AuthService: Main authentication service
- JWTProvider: Handles token generation and validation
- UserRepository: Manages user data storage

## Flow
1. User submits credentials
2. AuthService validates credentials via UserRepository
3. JWTProvider generates access token
4. Token returned to client
""",
    "/docs/users.md": """# User Management

The user management module handles user lifecycle operations.

## Features
- User registration and onboarding
- Profile management
- Role-based access control (RBAC)

## Integration with Auth
The UserRepository is shared with the Authentication system.
Users must be authenticated before accessing their profile.

## Data Model
- User: Core user entity with name, email, roles
- Profile: Extended user information
- Role: Permission groupings
""",
    "/src/auth/service.py": """
class AuthService:
    '''Main authentication service.

    Handles user login, logout, and token management.
    Integrates with JWTProvider for token operations.
    '''

    def __init__(self, user_repo, jwt_provider):
        self.user_repo = user_repo
        self.jwt_provider = jwt_provider

    async def authenticate(self, username: str, password: str):
        '''Authenticate user with credentials.'''
        user = await self.user_repo.find_by_username(username)
        if user and user.verify_password(password):
            return self.jwt_provider.create_token(user)
        return None
""",
    "/src/users/repository.py": """
class UserRepository:
    '''Repository for user data access.

    Provides CRUD operations for User entities.
    Used by both AuthService and UserService.
    '''

    def __init__(self, session):
        self.session = session

    async def find_by_username(self, username: str):
        '''Find user by username.'''
        pass

    async def find_by_email(self, email: str):
        '''Find user by email.'''
        pass

    async def create_user(self, user_data: dict):
        '''Create new user.'''
        pass
""",
}


class GraphRetrievalE2ETest:
    """End-to-end test runner for GraphEnhancedRetriever."""

    def __init__(self, database_url: str, zone_id: str = "graph-retrieval-e2e"):
        self.database_url = database_url
        self.zone_id = zone_id
        self.engine = None
        self.session = None
        self.graph_store = None
        self.semantic_search = None
        self.retriever = None
        self.passed = 0
        self.failed = 0
        self.temp_dir = None

    async def setup(self):
        """Set up database connection, create tables, and initialize components."""
        # Convert to async URL
        async_url = self.database_url
        if async_url.startswith("postgresql://"):
            async_url = async_url.replace("postgresql://", "postgresql+asyncpg://")
        elif async_url.startswith("sqlite:///"):
            async_url = async_url.replace("sqlite:///", "sqlite+aiosqlite:///")

        logger.info(f"Connecting to database: {async_url[:50]}...")
        self.engine = create_async_engine(async_url, echo=False)

        # Create tables
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables ready")

        # Create session
        async_session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self.session = async_session_factory()

        # Initialize components
        self.graph_store = GraphStore(self.session, zone_id=self.zone_id)
        logger.info(f"GraphStore initialized with zone_id={self.zone_id}")

        # Create temp directory for test files
        self.temp_dir = tempfile.mkdtemp(prefix="nexus_graph_retrieval_test_")
        logger.info(f"Created temp directory: {self.temp_dir}")

        # Write test documents to disk
        for path, content in TEST_DOCUMENTS.items():
            full_path = Path(self.temp_dir) / path.lstrip("/")
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content)
        logger.info(f"Created {len(TEST_DOCUMENTS)} test documents")

    async def teardown(self):
        """Clean up test data and close connections."""
        if self.session:
            logger.info("Cleaning up test data...")
            try:
                await self.session.execute(
                    text(
                        "DELETE FROM entity_mentions WHERE entity_id IN "
                        "(SELECT entity_id FROM entities WHERE zone_id = :zone)"
                    ),
                    {"zone": self.zone_id},
                )
                await self.session.execute(
                    text("DELETE FROM relationships WHERE zone_id = :zone"),
                    {"zone": self.zone_id},
                )
                await self.session.execute(
                    text("DELETE FROM entities WHERE zone_id = :zone"),
                    {"zone": self.zone_id},
                )
                await self.session.commit()
            except Exception as e:
                logger.warning(f"Cleanup error (may be expected): {e}")

            await self.session.close()

        if self.engine:
            await self.engine.dispose()

        # Clean up temp directory
        if self.temp_dir:
            import shutil

            shutil.rmtree(self.temp_dir, ignore_errors=True)
            logger.info("Cleaned up temp directory")

    def _check(self, name: str, condition: bool, message: str = ""):
        """Check a test condition and log result."""
        if condition:
            self.passed += 1
            logger.info(f"  ✓ {name}")
        else:
            self.failed += 1
            logger.error(f"  ✗ {name}: {message}")

    async def test_config_validation(self):
        """Test GraphRetrievalConfig validation."""
        logger.info("\n=== Test: Config Validation ===")

        # Valid modes
        for mode in ["none", "low", "high", "dual"]:
            try:
                config = GraphRetrievalConfig(graph_mode=mode)
                self._check(f"Valid mode '{mode}'", config.graph_mode == mode)
            except ValueError:
                self._check(f"Valid mode '{mode}'", False, "Raised ValueError")

        # Invalid mode
        try:
            GraphRetrievalConfig(graph_mode="invalid")
            self._check("Invalid mode rejected", False, "Should have raised ValueError")
        except ValueError:
            self._check("Invalid mode rejected", True)

        # Lambda weights
        config = GraphRetrievalConfig(
            lambda_semantic=0.5,
            lambda_keyword=0.3,
            lambda_graph=0.2,
        )
        total = config.lambda_semantic + config.lambda_keyword + config.lambda_graph
        self._check("Lambda weights sum to 1.0", abs(total - 1.0) < 0.001)

    async def test_graph_enhanced_fusion_function(self):
        """Test the graph_enhanced_fusion function directly."""
        logger.info("\n=== Test: Graph-Enhanced Fusion ===")

        # Test data
        keyword_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test1",
                "score": 0.8,
            },
            {
                "chunk_id": "c2",
                "path": "/b.md",
                "chunk_index": 0,
                "chunk_text": "test2",
                "score": 0.6,
            },
        ]
        vector_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test1",
                "score": 0.7,
            },
            {
                "chunk_id": "c3",
                "path": "/c.md",
                "chunk_index": 0,
                "chunk_text": "test3",
                "score": 0.9,
            },
        ]

        # Test without graph boost
        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=vector_results,
            graph_boost_ids=set(),
            theme_boost_ids=set(),
            lambda_semantic=0.4,
            lambda_keyword=0.3,
            lambda_graph=0.3,
        )
        self._check("Fusion returns results", len(results) == 3)
        self._check("Results have scores", all("score" in r for r in results))

        # Test with graph boost
        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=vector_results,
            graph_boost_ids={"c2"},  # Boost c2 with entity match
            theme_boost_ids=set(),
            lambda_semantic=0.3,
            lambda_keyword=0.3,
            lambda_graph=0.4,
        )
        c2_result = next((r for r in results if r["chunk_id"] == "c2"), None)
        self._check("Graph boost applied to c2", c2_result is not None)
        self._check("c2 has graph_score=1.0", c2_result and c2_result.get("graph_score") == 1.0)

        # Test theme boost (0.7)
        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=vector_results,
            graph_boost_ids=set(),
            theme_boost_ids={"c3"},  # Theme match
        )
        c3_result = next((r for r in results if r["chunk_id"] == "c3"), None)
        self._check("Theme boost applied to c3", c3_result is not None)
        self._check("c3 has graph_score=0.7", c3_result and c3_result.get("graph_score") == 0.7)

    async def test_entity_operations(self):
        """Test entity creation and retrieval in GraphStore."""
        logger.info("\n=== Test: Entity Operations ===")

        # Create test entities
        entities = [
            ("AuthService", "CLASS"),
            ("JWTProvider", "CLASS"),
            ("UserRepository", "CLASS"),
            ("authenticate", "FUNCTION"),
            ("JWT", "CONCEPT"),
            ("OAuth2", "CONCEPT"),
        ]

        created_ids = {}
        for name, entity_type in entities:
            entity_id, is_new = await self.graph_store.add_entity(
                name=name,
                entity_type=entity_type,
            )
            created_ids[name] = entity_id
            self._check(f"Created entity '{name}'", entity_id is not None)

        # Add relationships
        relationships = [
            ("AuthService", "JWTProvider", "USES"),
            ("AuthService", "UserRepository", "USES"),
            ("AuthService", "authenticate", "HAS_METHOD"),
            ("JWTProvider", "JWT", "CREATES"),
            ("AuthService", "OAuth2", "SUPPORTS"),
        ]

        for source, target, rel_type in relationships:
            if source in created_ids and target in created_ids:
                rel_id = await self.graph_store.add_relationship(
                    source_entity_id=created_ids[source],
                    target_entity_id=created_ids[target],
                    relationship_type=rel_type,
                )
                self._check(
                    f"Created relationship {source}->{rel_type}->{target}", rel_id is not None
                )

        # Test neighbor traversal
        auth_id = created_ids.get("AuthService")
        if auth_id:
            neighbors = await self.graph_store.get_neighbors(
                entity_id=auth_id,
                hops=1,
                direction="both",
            )
            self._check("Get neighbors returns results", len(neighbors) > 0)
            neighbor_names = [n.entity.canonical_name for n in neighbors]
            self._check("JWTProvider is neighbor", "JWTProvider" in neighbor_names)
            self._check("UserRepository is neighbor", "UserRepository" in neighbor_names)

        # Test subgraph extraction
        if auth_id:
            subgraph = await self.graph_store.get_subgraph(
                entity_ids=[auth_id],
                max_hops=1,
            )
            self._check("Subgraph has entities", len(subgraph.entities) > 0)
            self._check("Subgraph has relationships", len(subgraph.relationships) > 0)

        await self.session.commit()

    async def test_retriever_mode_none(self):
        """Test GraphEnhancedRetriever with mode='none'."""
        logger.info("\n=== Test: Retriever Mode 'none' ===")

        # Create mock semantic search
        class MockSemanticSearch:
            embedding_provider = None

            async def search(self, _query, _path="/", _limit=10, _search_mode="hybrid", _alpha=0.5):
                return [
                    SemanticSearchResult(
                        path="/docs/auth.md",
                        chunk_index=0,
                        chunk_text="JWT authentication...",
                        score=0.85,
                        keyword_score=0.7,
                        vector_score=0.9,
                    ),
                ]

        mock_search = MockSemanticSearch()
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_search,
            config=GraphRetrievalConfig(graph_mode="none"),
        )

        results = await retriever.search("authentication")
        self._check("Mode 'none' returns results", len(results) > 0)
        self._check(
            "Results are GraphEnhancedSearchResult",
            isinstance(results[0], GraphEnhancedSearchResult),
        )
        self._check("Graph score is None (mode=none)", results[0].graph_score is None)

    async def test_retriever_mode_low(self):
        """Test GraphEnhancedRetriever with mode='low' (entity-based)."""
        logger.info("\n=== Test: Retriever Mode 'low' ===")

        # Create mock embedding provider
        class MockEmbeddingProvider:
            async def embed_texts(self, texts):
                return [[0.1] * 384 for _ in texts]

        # Create mock semantic search
        class MockSemanticSearch:
            embedding_provider = MockEmbeddingProvider()

            async def search(self, _query, _path="/", _limit=10, _search_mode="hybrid", _alpha=0.5):
                return [
                    SemanticSearchResult(
                        path="/docs/auth.md",
                        chunk_index=0,
                        chunk_text="JWT authentication with AuthService...",
                        score=0.85,
                        keyword_score=0.7,
                        vector_score=0.9,
                    ),
                ]

        mock_search = MockSemanticSearch()

        # Add entity mention for testing
        auth_entity = await self.graph_store.find_entity("AuthService")
        if auth_entity:
            await self.graph_store.add_mention(
                entity_id=auth_entity.entity_id,
                chunk_id="/docs/auth.md:0",
            )
            await self.session.commit()

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_search,
            graph_store=self.graph_store,
            embedding_provider=MockEmbeddingProvider(),
            config=GraphRetrievalConfig(graph_mode="low", neighbor_hops=1),
        )

        results = await retriever.search("AuthService authentication")
        self._check("Mode 'low' returns results", len(results) > 0)
        self._check("Results have graph_score", results[0].graph_score is not None)

    async def test_performance(self):
        """Test search latency."""
        logger.info("\n=== Test: Performance ===")

        # Create mock components
        class MockEmbeddingProvider:
            async def embed_texts(self, texts):
                return [[0.1] * 384 for _ in texts]

        class MockSemanticSearch:
            embedding_provider = MockEmbeddingProvider()

            async def search(self, _query, _path="/", limit=10, _search_mode="hybrid", _alpha=0.5):
                return [
                    SemanticSearchResult(
                        path="/docs/auth.md",
                        chunk_index=i,
                        chunk_text=f"Test chunk {i}",
                        score=0.9 - i * 0.05,
                        keyword_score=0.8 - i * 0.05,
                        vector_score=0.9 - i * 0.05,
                    )
                    for i in range(min(limit, 10))
                ]

        mock_search = MockSemanticSearch()
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_search,
            graph_store=self.graph_store,
            embedding_provider=MockEmbeddingProvider(),
            config=GraphRetrievalConfig(graph_mode="low"),
        )

        # Warm up
        await retriever.search("test query")

        # Measure latency
        latencies = []
        for i in range(5):
            start = time.perf_counter()
            await retriever.search(f"test query {i}")
            latency = (time.perf_counter() - start) * 1000
            latencies.append(latency)

        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)

        logger.info(f"  Latencies: {[f'{lat:.2f}ms' for lat in latencies]}")
        logger.info(f"  Average: {avg_latency:.2f}ms, Max: {max_latency:.2f}ms")

        self._check("Average latency < 500ms", avg_latency < 500)
        self._check("Max latency < 1000ms", max_latency < 1000)

    async def run_all_tests(self):
        """Run all E2E tests."""
        logger.info("=" * 60)
        logger.info("Graph-Enhanced Retrieval E2E Tests (Issue #1040)")
        logger.info("=" * 60)

        await self.test_config_validation()
        await self.test_graph_enhanced_fusion_function()
        await self.test_entity_operations()
        await self.test_retriever_mode_none()
        await self.test_retriever_mode_low()
        await self.test_performance()

        logger.info("\n" + "=" * 60)
        logger.info(f"Results: {self.passed} passed, {self.failed} failed")
        logger.info("=" * 60)

        return self.failed == 0


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Graph-Enhanced Retrieval E2E Tests")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("NEXUS_DATABASE_URL", "sqlite:///test_graph_retrieval.db"),
        help="Database URL (default: NEXUS_DATABASE_URL env or SQLite)",
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

    test = GraphRetrievalE2ETest(database_url=args.database_url)

    try:
        await test.setup()
        success = await test.run_all_tests()
        sys.exit(0 if success else 1)
    finally:
        await test.teardown()


if __name__ == "__main__":
    asyncio.run(main())
