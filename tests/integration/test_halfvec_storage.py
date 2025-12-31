"""Integration test for halfvec (float16) storage.

This test verifies Issue #948: Convert vector storage from float32 to float16.

Tests:
1. Embedding column is created as halfvec(1536) not vector(1536)
2. HNSW index uses halfvec_cosine_ops not vector_cosine_ops
3. Embeddings can be stored and retrieved correctly
4. Similarity search works with halfvec
5. Storage size is approximately 50% of float32
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Skip if no test database URL
pytestmark = pytest.mark.skipif(
    not os.getenv("HALFVEC_TEST_DATABASE_URL"),
    reason="HALFVEC_TEST_DATABASE_URL not set",
)


@pytest.fixture(scope="module")
def engine():
    """Create test database engine."""
    db_url = os.getenv("HALFVEC_TEST_DATABASE_URL")
    return create_engine(db_url)


@pytest.fixture(scope="module")
def session_factory(engine):
    """Create session factory."""
    return sessionmaker(bind=engine)


@pytest.fixture(scope="function")
def session(session_factory):
    """Create a test session."""
    sess = session_factory()
    yield sess
    sess.close()


@pytest.fixture(scope="module", autouse=True)
def setup_database(engine):
    """Set up test database with pgvector extension."""
    from nexus.search.vector_db import VectorDatabase
    from nexus.storage.models import Base

    with engine.connect() as conn:
        # Enable pgvector extension
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()

    # Create tables
    Base.metadata.create_all(engine)

    # Initialize vector database (this should create halfvec column)
    vector_db = VectorDatabase(engine)
    vector_db.initialize()

    yield

    # Cleanup
    Base.metadata.drop_all(engine)


class TestHalfvecColumn:
    """Test that embedding column is halfvec type."""

    def test_column_type_is_halfvec(self, session):
        """Verify embedding column is halfvec, not vector."""
        result = session.execute(
            text("""
                SELECT udt_name
                FROM information_schema.columns
                WHERE table_name = 'document_chunks'
                  AND column_name = 'embedding'
            """)
        )
        row = result.fetchone()
        assert row is not None, "Embedding column not found"
        assert row[0] == "halfvec", f"Expected halfvec, got {row[0]}"

    def test_hnsw_index_operator_class(self, session):
        """Verify HNSW index uses halfvec_cosine_ops."""
        result = session.execute(
            text("""
                SELECT opcname
                FROM pg_index i
                JOIN pg_class c ON i.indexrelid = c.oid
                JOIN pg_opclass o ON i.indclass[0] = o.oid
                WHERE c.relname = 'idx_chunks_embedding_hnsw'
            """)
        )
        row = result.fetchone()
        assert row is not None, "HNSW index not found"
        assert row[0] == "halfvec_cosine_ops", f"Expected halfvec_cosine_ops, got {row[0]}"


class TestHalfvecStorage:
    """Test storing and retrieving halfvec embeddings."""

    def test_store_embedding(self, session, engine):
        """Test storing an embedding as halfvec."""
        import uuid

        from nexus.search.vector_db import VectorDatabase
        from nexus.storage.models import DocumentChunkModel, FilePathModel

        vector_db = VectorDatabase(engine)

        # Create a test file path
        path_id = str(uuid.uuid4())
        file_path = FilePathModel(
            path_id=path_id,
            tenant_id="test",
            virtual_path="/test/halfvec_test.txt",
            backend_id="test-backend",
            physical_path="/tmp/halfvec_test.txt",
            file_type="text",
            size_bytes=100,
        )
        session.add(file_path)
        session.flush()

        # Create a test chunk
        chunk_id = str(uuid.uuid4())
        chunk = DocumentChunkModel(
            chunk_id=chunk_id,
            path_id=path_id,
            chunk_index=0,
            chunk_text="Test chunk for halfvec storage",
            chunk_tokens=10,
        )
        session.add(chunk)
        session.flush()

        # Create a test embedding (1536 dimensions like OpenAI)
        embedding = [0.1] * 1536

        # Store embedding
        vector_db.store_embedding(session, chunk_id, embedding)
        session.commit()

        # Verify embedding was stored
        result = session.execute(
            text("""
                SELECT
                    pg_column_size(embedding) as size_bytes,
                    embedding IS NOT NULL as has_embedding
                FROM document_chunks
                WHERE chunk_id = :chunk_id
            """),
            {"chunk_id": chunk_id},
        )
        row = result.fetchone()

        assert row is not None, "Chunk not found"
        assert row.has_embedding, "Embedding not stored"

        # halfvec should be ~3KB for 1536 dims (2 bytes/dim + overhead)
        # vector would be ~6KB (4 bytes/dim + overhead)
        assert row.size_bytes < 4000, (
            f"Embedding too large ({row.size_bytes} bytes), expected halfvec ~3KB"
        )
        assert row.size_bytes > 2500, (
            f"Embedding too small ({row.size_bytes} bytes), expected halfvec ~3KB"
        )

    def test_similarity_search(self, session, engine):
        """Test that similarity search works with halfvec."""
        import uuid

        from nexus.search.vector_db import VectorDatabase
        from nexus.storage.models import DocumentChunkModel, FilePathModel

        vector_db = VectorDatabase(engine)

        # Create test data
        path_id = str(uuid.uuid4())
        file_path = FilePathModel(
            path_id=path_id,
            tenant_id="test",
            virtual_path="/test/search_test.txt",
            backend_id="test-backend",
            physical_path="/tmp/search_test.txt",
            file_type="text",
            size_bytes=100,
        )
        session.add(file_path)
        session.flush()

        # Create multiple chunks with different embeddings
        embeddings = [
            [0.9] + [0.1] * 1535,  # Similar to query
            [0.1] * 1536,  # Different from query
            [0.8] + [0.15] * 1535,  # Somewhat similar
        ]

        chunk_ids = []
        for i, emb in enumerate(embeddings):
            chunk_id = str(uuid.uuid4())
            chunk_ids.append(chunk_id)
            chunk = DocumentChunkModel(
                chunk_id=chunk_id,
                path_id=path_id,
                chunk_index=i,
                chunk_text=f"Test chunk {i}",
                chunk_tokens=10,
            )
            session.add(chunk)
            session.flush()
            vector_db.store_embedding(session, chunk_id, emb)

        session.commit()

        # Search with query similar to first embedding
        query_embedding = [0.85] + [0.12] * 1535

        results = vector_db.vector_search(session, query_embedding, limit=3)

        assert len(results) == 3, f"Expected 3 results, got {len(results)}"

        # First result should be most similar (chunk 0)
        assert results[0]["chunk_id"] == chunk_ids[0], "Most similar chunk should be first"

        # Scores should be in descending order
        assert results[0]["score"] >= results[1]["score"] >= results[2]["score"]


class TestHalfvecStorageSize:
    """Test storage size reduction with halfvec."""

    def test_storage_size_reduction(self, session, engine):
        """Verify that halfvec uses approximately 50% less storage than vector."""
        import uuid

        from nexus.search.vector_db import VectorDatabase
        from nexus.storage.models import DocumentChunkModel, FilePathModel

        vector_db = VectorDatabase(engine)

        # Create test file path
        path_id = str(uuid.uuid4())
        file_path = FilePathModel(
            path_id=path_id,
            tenant_id="test",
            virtual_path="/test/size_test.txt",
            backend_id="test-backend",
            physical_path="/tmp/size_test.txt",
            file_type="text",
            size_bytes=100,
        )
        session.add(file_path)
        session.flush()

        # Create 100 chunks with embeddings
        for i in range(100):
            chunk_id = str(uuid.uuid4())
            chunk = DocumentChunkModel(
                chunk_id=chunk_id,
                path_id=path_id,
                chunk_index=i,
                chunk_text=f"Test chunk {i} for storage size measurement",
                chunk_tokens=10,
            )
            session.add(chunk)
            session.flush()

            # Random-ish embedding
            embedding = [(i * j % 100) / 100.0 for j in range(1536)]
            vector_db.store_embedding(session, chunk_id, embedding)

        session.commit()

        # Measure storage
        result = session.execute(
            text("""
                SELECT
                    COUNT(*) as num_embeddings,
                    AVG(pg_column_size(embedding))::int as avg_bytes
                FROM document_chunks
                WHERE embedding IS NOT NULL
            """)
        )
        row = result.fetchone()

        # Expected: ~3072 bytes for halfvec (1536 * 2)
        # Float32 would be: ~6144 bytes (1536 * 4)
        expected_halfvec_size = 1536 * 2 + 8  # 2 bytes/dim + overhead
        expected_vector_size = 1536 * 4 + 8  # 4 bytes/dim + overhead

        print("\nStorage measurements:")
        print(f"  - Number of embeddings: {row.num_embeddings}")
        print(f"  - Average bytes per embedding: {row.avg_bytes}")
        print(f"  - Expected halfvec size: ~{expected_halfvec_size} bytes")
        print(f"  - Expected vector size: ~{expected_vector_size} bytes")

        # Verify halfvec is being used (should be close to 3KB, not 6KB)
        assert row.avg_bytes < 4000, f"Storage too large ({row.avg_bytes} bytes), not using halfvec"
        assert row.avg_bytes > 2500, f"Storage too small ({row.avg_bytes} bytes), unexpected"

        # Calculate approximate savings
        savings_percent = (1 - row.avg_bytes / expected_vector_size) * 100
        print(f"  - Storage savings vs float32: {savings_percent:.1f}%")
        assert savings_percent > 40, f"Expected >40% savings, got {savings_percent:.1f}%"


if __name__ == "__main__":
    # Quick test runner for manual testing
    import subprocess
    import sys

    # Start test database container
    print("Starting pgvector test container...")
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "halfvec-test-postgres",
            "-e",
            "POSTGRES_PASSWORD=test",
            "-e",
            "POSTGRES_DB=halfvec_test",
            "-p",
            "5434:5432",
            "pgvector/pgvector:pg18",
        ],
        check=True,
    )

    print("Waiting for container...")
    import time

    time.sleep(5)

    try:
        # Set test database URL
        os.environ["HALFVEC_TEST_DATABASE_URL"] = (
            "postgresql://postgres:test@localhost:5434/halfvec_test"
        )

        # Run tests
        sys.exit(pytest.main([__file__, "-v"]))
    finally:
        # Cleanup
        print("Cleaning up test container...")
        subprocess.run(["docker", "stop", "halfvec-test-postgres"], check=False)
        subprocess.run(["docker", "rm", "halfvec-test-postgres"], check=False)
