"""PostgreSQL Metadata Store Demo

This example demonstrates how to use Nexus with PostgreSQL instead of SQLite.

Requirements:
    pip install nexus-ai-fs[postgres]

PostgreSQL Setup:
    # Using Docker (easiest way to get started)
    docker run --name nexus-postgres -e POSTGRES_PASSWORD=nexus -e POSTGRES_DB=nexus -p 5432:5432 -d postgres:15

    # Or use an existing PostgreSQL instance

Environment Variables:
    NEXUS_DATABASE_URL - Full database URL (e.g., postgresql://user:pass@host/db)
    POSTGRES_URL - Alternative to NEXUS_DATABASE_URL

Usage:
    # Using environment variable
    export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost/nexus"
    python examples/postgres_demo.py

    # Or set in .env file
    echo "NEXUS_DATABASE_URL=postgresql://postgres:nexus@localhost/nexus" > .env
    python examples/postgres_demo.py
"""

import os
from datetime import UTC, datetime

from nexus.core.metadata import FileMetadata
from nexus.storage.metadata_store import SQLAlchemyMetadataStore


def main() -> None:
    """Demo PostgreSQL metadata store usage."""

    # Option 1: Use environment variable (recommended)
    # export NEXUS_DATABASE_URL="postgresql://postgres:nexus@localhost/nexus"
    db_url = os.getenv("NEXUS_DATABASE_URL") or os.getenv("POSTGRES_URL")

    # Option 2: Pass URL directly (for testing)
    if not db_url:
        db_url = "postgresql://postgres:nexus@localhost/nexus"
        print(f"⚠️  No NEXUS_DATABASE_URL set, using default: {db_url}")
        print("   For production, set NEXUS_DATABASE_URL environment variable")
        print()

    print("=" * 70)
    print("Nexus PostgreSQL Metadata Store Demo")
    print("=" * 70)
    print(f"Database URL: {db_url.split('@')[0]}@***")  # Hide password
    print()

    # Create metadata store with PostgreSQL
    print("📦 Initializing PostgreSQL metadata store...")
    try:
        store = SQLAlchemyMetadataStore(db_url=db_url)
        print(f"✅ Connected to PostgreSQL (type: {store.db_type})")
        print(f"   Pool size: {store.engine.pool.size()}")
        print()
    except Exception as e:
        print(f"❌ Failed to connect to PostgreSQL: {e}")
        print()
        print("Make sure PostgreSQL is running:")
        print("  docker run --name nexus-postgres \\")
        print("    -e POSTGRES_PASSWORD=nexus \\")
        print("    -e POSTGRES_DB=nexus \\")
        print("    -p 5432:5432 -d postgres:15")
        print()
        return

    try:
        # Create some test data
        print("📝 Writing test metadata...")
        test_files = [
            FileMetadata(
                path="/workspace/demo/file1.txt",
                backend_name="local",
                physical_path="/tmp/file1.txt",
                size=1024,
                etag="abc123",
                mime_type="text/plain",
                created_at=datetime.now(UTC),
                modified_at=datetime.now(UTC),
            ),
            FileMetadata(
                path="/workspace/demo/file2.json",
                backend_name="s3",
                physical_path="s3://bucket/file2.json",
                size=2048,
                etag="def456",
                mime_type="application/json",
                created_at=datetime.now(UTC),
                modified_at=datetime.now(UTC),
            ),
            FileMetadata(
                path="/workspace/demo/data/file3.csv",
                backend_name="gcs",
                physical_path="gs://bucket/file3.csv",
                size=4096,
                etag="ghi789",
                mime_type="text/csv",
                created_at=datetime.now(UTC),
                modified_at=datetime.now(UTC),
            ),
        ]

        # Batch insert (efficient for PostgreSQL)
        store.put_batch(test_files)
        print(f"✅ Inserted {len(test_files)} files using batch operation")
        print()

        # Query data
        print("🔍 Querying metadata...")
        result = store.get("/workspace/demo/file1.txt")
        if result:
            print(f"   Found: {result.path}")
            print(f"   Backend: {result.backend_name}")
            print(f"   Size: {result.size} bytes")
            print(f"   ETag: {result.etag}")
        print()

        # List files with prefix
        print("📋 Listing files with prefix '/workspace/demo/'...")
        files = store.list("/workspace/demo/")
        for file in files:
            print(f"   • {file.path} ({file.size} bytes, {file.backend_name})")
        print(f"   Total: {len(files)} files")
        print()

        # Batch get content IDs (for deduplication)
        print("🔗 Batch getting content IDs (deduplication)...")
        paths = [f.path for f in test_files]
        content_ids = store.batch_get_content_ids(paths)
        for path, content_id in content_ids.items():
            print(f"   • {path.split('/')[-1]}: {content_id}")
        print()

        # Cache statistics
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
            if cache_stats["ttl_seconds"]:
                print(f"   TTL: {cache_stats['ttl_seconds']}s")
            print()

        # Connection pool info
        print("🔌 PostgreSQL Connection Pool:")
        print(f"   Active connections: {store.engine.pool.checkedout()}")
        print(f"   Pool size: {store.engine.pool.size()}")
        print()

        print("=" * 70)
        print("✅ PostgreSQL demo completed successfully!")
        print("=" * 70)
        print()
        print("Key Benefits of PostgreSQL over SQLite:")
        print("  • Better concurrency (multiple writers)")
        print("  • Connection pooling for better performance")
        print("  • Remote database support")
        print("  • Better suited for production workloads")
        print("  • ACID compliance with better isolation")
        print()

    finally:
        # Clean up
        print("🧹 Cleaning up test data...")
        try:
            paths_to_delete = [f.path for f in test_files]
            store.delete_batch(paths_to_delete)
            print("✅ Test data cleaned up")
        except Exception as e:
            print(f"⚠️  Cleanup error (non-critical): {e}")

        store.close()
        print("🔒 Database connection closed")


if __name__ == "__main__":
    main()
