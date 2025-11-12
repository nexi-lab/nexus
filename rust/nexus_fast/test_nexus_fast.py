#!/usr/bin/env python3
"""
Test script for nexus_fast Rust extension
"""

import time
from typing import Any

import nexus_fast


def test_basic_permission() -> None:
    """Test basic direct permission check"""
    print("Test 1: Basic direct permission...")

    checks = [
        (("user", "alice"), "read", ("file", "doc1")),
    ]

    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": "doc1",
        }
    ]

    namespace_configs: dict[str, Any] = {}

    result = nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)
    print(f"  Result: {result}")
    assert result[("user", "alice", "read", "file", "doc1")]
    print("  ✓ Passed")


def test_permission_with_namespace() -> None:
    """Test permission with namespace configuration"""
    print("\nTest 2: Permission with namespace (union relation)...")

    checks = [
        (("user", "alice"), "editor", ("file", "doc1")),
    ]

    tuples = [
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "writer",
            "object_type": "file",
            "object_id": "doc1",
        }
    ]

    namespace_configs = {
        "file": {
            "relations": {
                "reader": "direct",
                "writer": "direct",
                "editor": {"union": ["reader", "writer"]},
            },
            "permissions": {},
        }
    }

    result = nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)
    print(f"  Result: {result}")
    assert result[("user", "alice", "editor", "file", "doc1")]
    print("  ✓ Passed")


def test_tuple_to_userset() -> None:
    """Test tuple-to-userset (parent relation)"""
    print("\nTest 3: TupleToUserset (parent folder permissions)...")

    checks = [
        (("user", "alice"), "read", ("file", "doc1")),
    ]

    tuples = [
        # doc1 is in folder1
        {
            "subject_type": "file",
            "subject_id": "doc1",
            "subject_relation": None,
            "relation": "parent",
            "object_type": "folder",
            "object_id": "folder1",
        },
        # alice can read folder1
        {
            "subject_type": "user",
            "subject_id": "alice",
            "subject_relation": None,
            "relation": "read",
            "object_type": "folder",
            "object_id": "folder1",
        },
    ]

    namespace_configs = {
        "file": {
            "relations": {
                "read": {"tupleToUserset": {"tupleset": "parent", "computedUserset": "read"}}
            },
            "permissions": {},
        },
        "folder": {"relations": {"read": "direct"}, "permissions": {}},
    }

    result = nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)
    print(f"  Result: {result}")
    assert result[("user", "alice", "read", "file", "doc1")]
    print("  ✓ Passed")


def test_bulk_performance() -> None:
    """Test bulk permission checking performance"""
    print("\nTest 4: Bulk performance test (1000 checks)...")

    # Create 1000 permission checks
    checks = []
    tuples = []

    for i in range(1000):
        checks.append((("user", f"user{i}"), "read", ("file", f"file{i}")))
        tuples.append(
            {
                "subject_type": "user",
                "subject_id": f"user{i}",
                "subject_relation": None,
                "relation": "read",
                "object_type": "file",
                "object_id": f"file{i}",
            }
        )

    namespace_configs: dict[str, Any] = {}

    start = time.time()
    result = nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)
    elapsed = time.time() - start

    print(f"  Processed {len(checks)} checks in {elapsed * 1000:.2f}ms")
    print(f"  Average: {elapsed / len(checks) * 1000000:.2f}µs per check")
    assert len(result) == 1000
    assert all(result[key] for key in result)
    print("  ✓ Passed")


def test_negative_case() -> None:
    """Test permission denial"""
    print("\nTest 5: Permission denial (negative case)...")

    checks = [
        (("user", "alice"), "read", ("file", "doc1")),
    ]

    tuples = [
        {
            "subject_type": "user",
            "subject_id": "bob",  # Different user
            "subject_relation": None,
            "relation": "read",
            "object_type": "file",
            "object_id": "doc1",
        }
    ]

    namespace_configs: dict[str, Any] = {}

    result = nexus_fast.compute_permissions_bulk(checks, tuples, namespace_configs)
    print(f"  Result: {result}")
    assert not result[("user", "alice", "read", "file", "doc1")]
    print("  ✓ Passed")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing nexus_fast Rust extension")
    print("=" * 60)

    test_basic_permission()
    test_permission_with_namespace()
    test_tuple_to_userset()
    test_bulk_performance()
    test_negative_case()

    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)
