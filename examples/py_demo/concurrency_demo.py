"""Optimistic Concurrency Control (OCC) Demo for Nexus.

This demo shows how to use Nexus's lock-free concurrency features
to safely handle multi-agent collaboration scenarios.

Features demonstrated:
1. Reading files with metadata (etag, version)
2. Conditional writes with version checking (if_match)
3. Conflict detection and resolution strategies
4. Create-only writes (if_none_match)
5. Force overwrites (dangerous!)
"""

import tempfile
from pathlib import Path

import nexus
from nexus.core.exceptions import ConflictError


def demo_basic_occ() -> None:
    """Demonstrate basic optimistic concurrency control."""
    print("=" * 70)
    print("Demo 1: Basic Optimistic Concurrency Control")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        nx = nexus.connect(config={"data_dir": str(data_dir)})

        # Write initial content
        print("\n1. Agent A writes initial content...")
        result = nx.write("/shared/document.txt", b"Version 1")
        print(f"   ✓ Written - ETag: {result['etag'][:16]}...")
        print(f"   ✓ Version: {result['version']}")

        # Read with metadata
        print("\n2. Agent B reads the file with metadata...")
        data = nx.read("/shared/document.txt", return_metadata=True)
        print(f"   Content: {data['content'].decode()}")
        print(f"   ETag: {data['etag'][:16]}...")
        print(f"   Version: {data['version']}")
        print(f"   Size: {data['size']} bytes")
        print(f"   Modified: {data['modified_at']}")

        # Conditional write with matching etag (succeeds)
        print("\n3. Agent B updates with correct etag (should succeed)...")
        result2 = nx.write(
            "/shared/document.txt", b"Version 2 - updated by Agent B", if_match=data["etag"]
        )
        print("   ✓ Write succeeded!")
        print(f"   New ETag: {result2['etag'][:16]}...")
        print(f"   New Version: {result2['version']}")

        # Attempt to write with stale etag (fails)
        print("\n4. Agent A tries to update with stale etag (should fail)...")
        try:
            nx.write(
                "/shared/document.txt",
                b"Version 2 - conflicting update by Agent A",
                if_match=result["etag"],  # This is the OLD etag from step 1
            )
            print("   ✗ ERROR: Should have raised ConflictError!")
        except ConflictError as e:
            print(f"   ✓ Conflict detected: {e.message}")
            print(f"   Expected ETag: {e.expected_etag[:16]}...")
            print(f"   Current ETag: {e.current_etag[:16]}...")

        nx.close()
        print("\n✓ Basic OCC demo completed!")


def demo_conflict_resolution_strategies() -> None:
    """Demonstrate different conflict resolution strategies."""
    print("\n" + "=" * 70)
    print("Demo 2: Conflict Resolution Strategies")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        nx = nexus.connect(config={"data_dir": str(data_dir)})

        # Setup: Create initial file
        result = nx.write("/shared/counter.txt", b"count: 0")
        initial_etag = result["etag"]

        # Strategy 1: Retry with fresh read
        print("\n--- Strategy 1: Retry with Fresh Read ---")
        print("1. Simulating Agent A and Agent B racing to update counter...")

        # Agent A updates
        agent_a_data = nx.read("/shared/counter.txt", return_metadata=True)
        result_a = nx.write("/shared/counter.txt", b"count: 1", if_match=agent_a_data["etag"])
        print(f"   Agent A: Updated to count=1, version={result_a['version']}")

        # Agent B tries to update with stale data
        print("\n2. Agent B attempts update with stale etag...")
        try:
            nx.write("/shared/counter.txt", b"count: 1 (Agent B)", if_match=initial_etag)
        except ConflictError:
            print("   ✓ Conflict detected by Agent B")
            print("\n3. Agent B retries with fresh read...")
            fresh_data = nx.read("/shared/counter.txt", return_metadata=True)
            print(f"   Fresh content: {fresh_data['content'].decode()}")
            result_b = nx.write(
                "/shared/counter.txt", b"count: 2 (Agent B retry)", if_match=fresh_data["etag"]
            )
            print(f"   ✓ Agent B succeeded with retry, version={result_b['version']}")

        # Strategy 2: Three-way merge
        print("\n--- Strategy 2: Three-Way Merge ---")
        nx.write("/shared/config.json", b'{"setting_a": 1, "setting_b": 2}')

        # Agent A reads and plans to modify setting_a
        agent_a_data = nx.read("/shared/config.json", return_metadata=True)
        print(f"1. Agent A reads: {agent_a_data['content'].decode()}")

        # Agent B updates setting_b (concurrent modification)
        agent_b_data = nx.read("/shared/config.json", return_metadata=True)
        nx.write(
            "/shared/config.json",
            b'{"setting_a": 1, "setting_b": 99}',
            if_match=agent_b_data["etag"],
        )
        print("2. Agent B updates setting_b to 99")

        # Agent A tries to update setting_a
        print("\n3. Agent A tries to update setting_a...")
        try:
            nx.write(
                "/shared/config.json",
                b'{"setting_a": 77, "setting_b": 2}',
                if_match=agent_a_data["etag"],
            )
        except ConflictError:
            print("   ✓ Conflict detected")
            print("\n4. Agent A performs three-way merge...")
            current_data = nx.read("/shared/config.json", return_metadata=True)
            print(f"   Current state: {current_data['content'].decode()}")
            # Merge: Keep Agent B's setting_b=99, apply Agent A's setting_a=77
            merged = b'{"setting_a": 77, "setting_b": 99}'
            result = nx.write("/shared/config.json", merged, if_match=current_data["etag"])
            print(f"   ✓ Merged successfully: {merged.decode()}")
            print(f"   Version: {result['version']}")

        # Strategy 3: Abort on conflict
        print("\n--- Strategy 3: Abort on Conflict ---")
        nx.write("/shared/critical.txt", b"Critical data v1")
        agent_data = nx.read("/shared/critical.txt", return_metadata=True)

        # Another agent modifies it
        nx.write("/shared/critical.txt", b"Critical data v2 (modified by another agent)")

        print("1. Agent tries to update critical file...")
        try:
            nx.write(
                "/shared/critical.txt",
                b"Critical data v1 - agent's changes",
                if_match=agent_data["etag"],
            )
        except ConflictError as e:
            print(f"   ✓ Conflict detected: {e.message}")
            print("   Agent decides to abort (data too critical to auto-merge)")
            print("   → Manual review required")

        # Strategy 4: Force overwrite (dangerous!)
        print("\n--- Strategy 4: Force Overwrite (Dangerous!) ---")
        print("1. Agent needs to force overwrite without version check...")
        result = nx.write(
            "/shared/critical.txt",
            b"Force overwritten - all previous changes lost!",
            force=True,  # Skip version check
        )
        print(f"   ⚠️  Force overwrite succeeded, version={result['version']}")
        print("   ⚠️  Any concurrent changes were silently lost!")

        nx.close()
        print("\n✓ Conflict resolution strategies demo completed!")


def demo_create_only_mode() -> None:
    """Demonstrate create-only writes using if_none_match."""
    import time

    print("\n" + "=" * 70)
    print("Demo 3: Create-Only Mode (if_none_match)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        nx = nexus.connect(config={"data_dir": str(data_dir)})

        # Use unique filename to avoid any state issues
        filename = f"/workspace/new_file_{int(time.time() * 1000)}.txt"

        # Create-only write (succeeds)
        print("\n1. Creating new file with if_none_match=True...")
        result = nx.write(filename, b"Initial content", if_none_match=True)
        print(f"   ✓ File created, version={result['version']}")

        # Try to create same file again (fails)
        print("\n2. Trying to create same file again...")
        try:
            nx.write(filename, b"Different content", if_none_match=True)
            print("   ✗ ERROR: Should have raised FileExistsError!")
        except FileExistsError as e:
            print(f"   ✓ Create failed (file exists): {e}")

        # Normal write still works
        print("\n3. Normal write (without if_none_match) still works...")
        result2 = nx.write(filename, b"Updated content")
        print(f"   ✓ File updated, version={result2['version']}")

        nx.close()
        print("\n✓ Create-only mode demo completed!")


def demo_multi_agent_collaboration() -> None:
    """Demonstrate realistic multi-agent collaboration scenario."""
    print("\n" + "=" * 70)
    print("Demo 4: Multi-Agent Collaboration Scenario")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"

        # Agent 1: Code analyzer
        print("\n--- Agent 1: Code Analyzer ---")
        nx1 = nexus.connect(config={"data_dir": str(data_dir), "agent_id": "code-analyzer"})
        nx1.write("/project/analysis.md", b"# Code Analysis\n\n(empty)")
        print("✓ Created analysis.md")

        # Agent 1 reads for analysis
        data1 = nx1.read("/project/analysis.md", return_metadata=True)
        print(f"✓ Read file (etag: {data1['etag'][:16]}...)")

        # Agent 2: Documentation writer (concurrent access)
        print("\n--- Agent 2: Documentation Writer ---")
        nx2 = nexus.connect(config={"data_dir": str(data_dir), "agent_id": "doc-writer"})
        data2 = nx2.read("/project/analysis.md", return_metadata=True)
        print(f"✓ Read same file (etag: {data2['etag'][:16]}...)")

        # Agent 2 updates first
        print("\n--- Agent 2 updates first ---")
        result2 = nx2.write(
            "/project/analysis.md",
            b"# Code Analysis\n\n## Documentation\n\nAdded docs section.",
            if_match=data2["etag"],
        )
        print(f"✓ Agent 2 wrote update (version {result2['version']})")

        # Flush Agent 1's cache to ensure it sees Agent 2's update
        if hasattr(nx1, "metadata") and hasattr(nx1.metadata, "_cache"):
            nx1.metadata._cache.invalidate_path("/project/analysis.md")

        # Agent 1 tries to update (conflict!)
        print("\n--- Agent 1 tries to update (will conflict) ---")
        agent1_update = b"# Code Analysis\n\n## Issues Found\n\n- Issue 1\n- Issue 2"
        try:
            nx1.write("/project/analysis.md", agent1_update, if_match=data1["etag"])
            print("✗ ERROR: Should have detected conflict!")
        except ConflictError as e:
            print("✓ Conflict detected!")
            print(f"  Expected: {e.expected_etag[:16]}...")
            print(f"  Current:  {e.current_etag[:16]}...")

        # Agent 1 resolves conflict by merging
        print("\n--- Agent 1 resolves conflict ---")
        current = nx1.read("/project/analysis.md", return_metadata=True)
        print(f"✓ Read current version: {current['version']}")

        # Merge both updates
        merged = (
            b"# Code Analysis\n\n"
            b"## Documentation\n\nAdded docs section.\n\n"
            b"## Issues Found\n\n- Issue 1\n- Issue 2"
        )
        result_merged = nx1.write("/project/analysis.md", merged, if_match=current["etag"])
        print(f"✓ Merged successfully (version {result_merged['version']})")

        # Verify final state
        print("\n--- Final State ---")
        final = nx1.read("/project/analysis.md", return_metadata=True)
        print(f"Content:\n{final['content'].decode()}")
        print(f"\nVersion: {final['version']}")
        print(f"Size: {final['size']} bytes")

        nx1.close()
        nx2.close()
        print("\n✓ Multi-agent collaboration demo completed!")


def main() -> None:
    """Run all concurrency demos."""
    print("\n")
    print("╔" + "═" * 68 + "╗")
    print("║" + " " * 10 + "Nexus Optimistic Concurrency Control Demo" + " " * 16 + "║")
    print("╚" + "═" * 68 + "╝")
    print()

    demo_basic_occ()
    demo_conflict_resolution_strategies()
    demo_create_only_mode()
    demo_multi_agent_collaboration()

    print("\n" + "=" * 70)
    print("✓ All concurrency demos completed successfully!")
    print("=" * 70)
    print("\nKey Takeaways:")
    print("  1. Always read with return_metadata=True to get etag for OCC")
    print("  2. Use if_match parameter to prevent concurrent modification conflicts")
    print("  3. Handle ConflictError with retry, merge, abort, or force strategies")
    print("  4. Use if_none_match=True for create-only operations")
    print("  5. Avoid force=True unless absolutely necessary (data loss risk!)")
    print()


if __name__ == "__main__":
    main()
