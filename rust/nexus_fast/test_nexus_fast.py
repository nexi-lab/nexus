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


def test_single_permission_check() -> None:
    """Test compute_permission_single function"""
    print("\nTest 6: Single permission check (new function)...")

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

    result = nexus_fast.compute_permission_single(
        "user", "alice", "read", "file", "doc1", tuples, namespace_configs
    )
    print(f"  Result: {result}")
    assert result is True
    print("  ✓ Passed")


def test_single_with_hierarchy() -> None:
    """Test single check with parent hierarchy"""
    print("\nTest 7: Single permission with parent hierarchy...")

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

    result = nexus_fast.compute_permission_single(
        "user", "alice", "read", "file", "doc1", tuples, namespace_configs
    )
    print(f"  Result: {result}")
    assert result is True
    print("  ✓ Passed")


def test_filter_paths_basic() -> None:
    """Test basic path filtering"""
    print("\nTest 8: Basic path filtering...")

    paths = [
        "file.txt",
        "._hidden.txt",
        ".DS_Store",
        "document.pdf",
        "Thumbs.db",
        "photo.jpg",
    ]

    exclude_patterns = ["._*", ".DS_Store", "Thumbs.db"]

    result = nexus_fast.filter_paths(paths, exclude_patterns)
    print(f"  Input: {len(paths)} paths")
    print(f"  Filtered: {len(result)} paths")
    print(f"  Result: {result}")

    expected = ["file.txt", "document.pdf", "photo.jpg"]
    assert result == expected, f"Expected {expected}, got {result}"
    print("  ✓ Passed")


def test_filter_paths_with_directories() -> None:
    """Test path filtering with full paths"""
    print("\nTest 9: Path filtering with directories...")

    paths = [
        "/workspace/file.txt",
        "/workspace/._metadata",
        "/workspace/.DS_Store",
        "/workspace/subfolder/document.pdf",
        "/workspace/subfolder/Thumbs.db",
    ]

    exclude_patterns = ["._*", ".DS_Store", "Thumbs.db"]

    result = nexus_fast.filter_paths(paths, exclude_patterns)
    print(f"  Filtered: {len(result)} paths")

    expected = [
        "/workspace/file.txt",
        "/workspace/subfolder/document.pdf",
    ]
    assert result == expected, f"Expected {expected}, got {result}"
    print("  ✓ Passed")


def test_filter_paths_performance() -> None:
    """Test path filtering performance with 10k paths"""
    print("\nTest 10: Path filtering performance (10k paths)...")

    # Create 10k paths with 10% OS metadata
    paths = []
    for i in range(10000):
        if i % 10 == 0:
            paths.append(f"/workspace/._hidden_{i}")
        elif i % 10 == 1:
            paths.append(f"/workspace/.DS_Store_{i}")
        else:
            paths.append(f"/workspace/file_{i}.txt")

    exclude_patterns = ["._*", ".DS_Store*"]

    start = time.time()
    result = nexus_fast.filter_paths(paths, exclude_patterns)
    elapsed = time.time() - start

    print(f"  Processed {len(paths)} paths in {elapsed * 1000:.2f}ms")
    print(f"  Filtered to {len(result)} paths")
    print(f"  Average: {elapsed / len(paths) * 1000000:.2f}µs per path")

    # Should filter out 20% of paths
    assert len(result) == 8000, f"Expected 8000 paths, got {len(result)}"
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
    test_single_permission_check()
    test_single_with_hierarchy()
    test_filter_paths_basic()
    test_filter_paths_with_directories()
    test_filter_paths_performance()

    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)
