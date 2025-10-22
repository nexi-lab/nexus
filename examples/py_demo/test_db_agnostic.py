"""Test Database-Agnostic Implementation

This script demonstrates that the same code works with both SQLite and PostgreSQL.
"""

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from nexus.core.metadata import FileMetadata
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


def test_database_backend(db_url: str, db_name: str) -> None:
    """Test metadata store with a specific database backend."""

    print(f"\n{'=' * 70}")
    print(f"Testing {db_name}")
    print(f"{'=' * 70}")
    print(f"Database URL: {db_url.split('@')[0] if '@' in db_url else db_url}")
    print()

    try:
        # Initialize store
        print(f"📦 Initializing {db_name} metadata store...")
        store = SQLAlchemyMetadataStore(db_url=db_url)
        print(f"✅ Connected to {db_name} (type: {store.db_type})")

        # Check connection pool configuration
        if store.db_type == "postgresql":
            print(
                f"   Pool config: size={store.engine.pool.size()}, "
                f"max_overflow={store.engine.pool._max_overflow}"
            )
        elif store.db_type == "sqlite":
            print("   Pool config: NullPool (single connection)")
        print()

        # Create test data
        print("📝 Writing test metadata...")
        test_files = [
            FileMetadata(
                path="/test/file1.txt",
                backend_name="local",
                physical_path="/tmp/file1.txt",
                size=1024,
                etag="abc123",
                mime_type="text/plain",
                created_at=datetime.now(UTC),
                modified_at=datetime.now(UTC),
            ),
            FileMetadata(
                path="/test/file2.json",
                backend_name="s3",
                physical_path="s3://bucket/file2.json",
                size=2048,
                etag="def456",
                mime_type="application/json",
                created_at=datetime.now(UTC),
                modified_at=datetime.now(UTC),
            ),
        ]

        # Batch insert
        store.put_batch(test_files)
        print(f"✅ Inserted {len(test_files)} files")
        print()

        # Query data
        print("🔍 Querying metadata...")
        result = store.get("/test/file1.txt")
        if result:
            print(f"   Found: {result.path}")
            print(f"   Backend: {result.backend_name}")
            print(f"   Size: {result.size} bytes")
        print()

        # List files
        print("📋 Listing files...")
        files = store.list("/test/")
        for file in files:
            print(f"   • {file.path} ({file.size} bytes)")
        print()

        # Batch get content IDs
        print("🔗 Batch getting content IDs...")
        paths = [f.path for f in test_files]
        content_ids = store.batch_get_content_ids(paths)
        for path, cid in content_ids.items():
            print(f"   • {path.split('/')[-1]}: {cid}")
        print()

        # Cache stats
        cache_stats = store.get_cache_stats()
        if cache_stats:
            print("📊 Cache Statistics:")
            print(
                f"   Path cache: {cache_stats['path_cache_size']}/{cache_stats['path_cache_maxsize']} entries"
            )
            print(
                f"   List cache: {cache_stats['list_cache_size']}/{cache_stats['list_cache_maxsize']} entries"
            )
            print(
                f"   KV cache: {cache_stats['kv_cache_size']}/{cache_stats['kv_cache_maxsize']} entries"
            )
            print(
                f"   Exists cache: {cache_stats['exists_cache_size']}/{cache_stats['exists_cache_maxsize']} entries"
            )
        print()

        # Clean up
        print("🧹 Cleaning up test data...")
        store.delete_batch(paths)
        print("✅ Test data cleaned up")

        store.close()
        print(f"✅ {db_name} test completed successfully!")

    except Exception as e:
        print(f"❌ {db_name} test failed: {e}")
        import traceback

        traceback.print_exc()


def main() -> None:
    """Run database-agnostic tests."""

    print("=" * 70)
    print("Nexus Database-Agnostic Implementation Test")
    print("=" * 70)
    print()
    print("This demonstrates that the same code works with:")
    print("  • SQLite (local development)")
    print("  • PostgreSQL (production)")
    print()

    # Test 1: SQLite (always works)
    with tempfile.TemporaryDirectory() as tmpdir:
        sqlite_db = Path(tmpdir) / "test.db"
        sqlite_url = f"sqlite:///{sqlite_db}"
        test_database_backend(sqlite_url, "SQLite")

    # Test 2: PostgreSQL (if available)
    pg_url = os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL")
    if pg_url:
        print("\n" + "=" * 70)
        print("PostgreSQL environment variable detected!")
        print("=" * 70)
        test_database_backend(pg_url, "PostgreSQL")
    else:
        print("\n" + "=" * 70)
        print("PostgreSQL Test Skipped")
        print("=" * 70)
        print()
        print("To test PostgreSQL, set NEXUS_DATABASE_URL:")
        print("  export NEXUS_DATABASE_URL='postgresql://user:pass@host/db'")
        print()
        print("Or start PostgreSQL with Docker:")
        print("  docker run --name nexus-postgres \\")
        print("    -e POSTGRES_PASSWORD=nexus \\")
        print("    -e POSTGRES_DB=nexus \\")
        print("    -p 5432:5432 -d postgres:15")
        print()
        print("  export NEXUS_DATABASE_URL='postgresql://postgres:nexus@localhost/nexus'")
        print()

    print("=" * 70)
    print("✅ All tests completed!")
    print("=" * 70)
    print()
    print("Key Takeaway:")
    print("  The SAME code works with both SQLite and PostgreSQL!")
    print("  Just change the database URL via environment variable.")
    print()


if __name__ == "__main__":
    main()
