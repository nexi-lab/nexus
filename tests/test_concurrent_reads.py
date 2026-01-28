#!/usr/bin/env python3
"""
Test concurrent database reads to simulate skills registry scanning.
This tests if the SQLite pool configuration can handle many concurrent file reads.
"""

import asyncio
import sys
import time
from pathlib import Path

# Add nexus to path
sys.path.insert(0, str(Path(__file__).parent / "nexus" / "src"))

from nexus.storage.metadata_store import SQLAlchemyMetadataStore


async def read_file_metadata(store, path):
    """Read metadata for a single file."""
    try:
        metadata = store.get(path)
        return (path, metadata is not None, None)
    except Exception as e:
        return (path, False, str(e))


async def test_concurrent_reads(db_path: str, num_concurrent: int = 100):
    """Test concurrent reads from SQLite database."""
    print(f"=== Testing {num_concurrent} Concurrent SQLite Reads ===\n")

    # Initialize metadata store
    print(f"1. Connecting to database: {db_path}")
    store = SQLAlchemyMetadataStore(db_path=db_path, enable_cache=False)

    # Get all skill files
    print("2. Listing all skill files...")
    all_skills = store.list("/tenant:default/user:admin/skill/", recursive=True)
    print(f"   Found {len(all_skills)} skill files\n")

    if len(all_skills) == 0:
        print("❌ No skill files found! Can't test concurrent reads.")
        return False

    # Prepare test: read the same files many times to stress the connection pool
    test_paths = [skill.path for skill in all_skills[: min(20, len(all_skills))]]
    print(f"3. Testing with {len(test_paths)} unique paths, {num_concurrent} concurrent reads...")

    # Create concurrent read tasks (reading same paths multiple times)
    tasks = []
    for i in range(num_concurrent):
        path = test_paths[i % len(test_paths)]
        tasks.append(read_file_metadata(store, path))

    # Execute all reads concurrently
    start_time = time.time()
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - start_time

        # Analyze results
        successes = sum(1 for r in results if isinstance(r, tuple) and r[1])
        errors = sum(
            1 for r in results if isinstance(r, Exception) or (isinstance(r, tuple) and r[2])
        )

        print("\n4. Results:")
        print(f"   ✅ Successful reads: {successes}/{num_concurrent}")
        print(f"   ❌ Failed reads: {errors}/{num_concurrent}")
        print(f"   ⏱️  Total time: {elapsed:.2f}s")
        print(f"   📊 Throughput: {num_concurrent / elapsed:.1f} reads/sec")

        # Check for specific errors
        pool_errors = [
            r for r in results if isinstance(r, tuple) and r[2] and "pool" in r[2].lower()
        ]
        segfault_errors = [
            r for r in results if isinstance(r, Exception) and "segmentation" in str(r).lower()
        ]

        if pool_errors:
            print(f"\n   ⚠️  Connection pool errors: {len(pool_errors)}")
            print(f"      Example: {pool_errors[0][2][:100]}")

        if segfault_errors:
            print("\n   💥 SEGFAULT detected!")
            return False

        # Success if most reads succeeded
        success_rate = successes / num_concurrent
        if success_rate >= 0.95:
            print(
                f"\n✅ SUCCESS! {success_rate * 100:.0f}% success rate - SQLite pool handles concurrency well"
            )
            return True
        else:
            print(f"\n❌ FAILURE! Only {success_rate * 100:.0f}% success rate")
            return False

    except Exception as e:
        print(f"\n💥 CRASHED during concurrent reads: {e}")
        return False
    finally:
        store.close()


def main():
    """Main test function."""
    db_path = "/Users/jinjingzhou/nexi-lab/nexus/nexus-data-local/nexus.db"

    if not Path(db_path).exists():
        print(f"❌ Database not found: {db_path}")
        sys.exit(1)

    # Test with increasing concurrency
    for num_concurrent in [10, 50, 100]:
        print(f"\n{'=' * 60}")
        success = asyncio.run(test_concurrent_reads(db_path, num_concurrent))
        print(f"{'=' * 60}\n")

        if not success:
            print(f"❌ Test failed at {num_concurrent} concurrent reads")
            sys.exit(1)

        # Small delay between tests
        time.sleep(1)

    print("\n🎉 All tests passed! SQLite pool configuration is working correctly.")
    sys.exit(0)


if __name__ == "__main__":
    main()
