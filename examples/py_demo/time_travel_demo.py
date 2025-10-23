"""Time-Travel Debugging Demo

This example demonstrates how to use Nexus's time-travel debugging features
to read files at historical operation points, enabling powerful debugging
and analysis of agent behavior over time.

Features:
- Read file content at any historical operation point
- List directory contents at historical points
- Compare file states between operations
- Understand what changed and when
"""

import tempfile
from pathlib import Path

import nexus


def time_travel_demo():
    """Demonstrate time-travel debugging features."""
    # Create temporary workspace
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Connect to Nexus
        nx = nexus.connect(config={"data_dir": str(data_dir)})

        print("=" * 70)
        print("Time-Travel Debugging Demo")
        print("=" * 70)
        print()

        # Simulate agent workflow with file evolution
        print("📝 Creating a file that evolves over time...")
        path = "/workspace/agent_log.txt"

        # Version 1: Initial agent log
        nx.write(path, b"Agent started\nInitializing workspace...")

        # Version 2: Agent makes progress
        nx.write(
            path,
            b"Agent started\nInitializing workspace...\nFetched data from API\nProcessing 100 records...",
        )

        # Version 3: Agent completes task
        nx.write(
            path,
            b"Agent started\nInitializing workspace...\nFetched data from API\n"
            b"Processing 100 records...\nTask completed successfully!\n"
            b"Results saved to output.json",
        )

        # Create another file to show directory listing
        nx.write("/workspace/output.json", b'{"status": "success", "records": 100}')

        print("✓ Created evolving file with 3 versions")
        print()

        # Demonstrate operation log
        print("=" * 70)
        print("1. Operation Log - What Happened and When?")
        print("=" * 70)

        from nexus.storage.operation_logger import OperationLogger

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # Get all operations for our file
            operations = logger.list_operations(path=path, limit=10)

            print(f"\nFound {len(operations)} operations for {path}:")
            print()

            for i, op in enumerate(reversed(operations)):
                print(f"  Version {i + 1}:")
                print(f"    Operation ID: {op.operation_id}")
                print(f"    Type: {op.operation_type}")
                print(f"    Time: {op.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"    Has snapshot: {op.snapshot_hash is not None}")
                print()

            # Save operation IDs for time-travel
            op_v1 = operations[2].operation_id  # Oldest
            op_v2 = operations[1].operation_id
            op_v3 = operations[0].operation_id  # Most recent

            # Demonstrate time-travel reading
            print("=" * 70)
            print("2. Time-Travel - Read File at Historical Points")
            print("=" * 70)

            from nexus.storage.time_travel import TimeTravelReader

            time_travel = TimeTravelReader(session, nx.backend)

            # Read at version 1
            print("\n🕐 Reading file at Version 1 (Initial state):")
            print("-" * 70)
            state_v1 = time_travel.get_file_at_operation(path, op_v1)
            content_v1 = state_v1["content"].decode("utf-8")
            print(content_v1)
            print("-" * 70)
            print(f"Size: {len(state_v1['content'])} bytes")
            print()

            # Read at version 2
            print("\n🕑 Reading file at Version 2 (Progress update):")
            print("-" * 70)
            state_v2 = time_travel.get_file_at_operation(path, op_v2)
            content_v2 = state_v2["content"].decode("utf-8")
            print(content_v2)
            print("-" * 70)
            print(f"Size: {len(state_v2['content'])} bytes")
            print()

            # Read at version 3
            print("\n🕒 Reading file at Version 3 (Final state):")
            print("-" * 70)
            state_v3 = time_travel.get_file_at_operation(path, op_v3)
            content_v3 = state_v3["content"].decode("utf-8")
            print(content_v3)
            print("-" * 70)
            print(f"Size: {len(state_v3['content'])} bytes")
            print()

            # Demonstrate operation diff
            print("=" * 70)
            print("3. Operation Diff - What Changed Between Versions?")
            print("=" * 70)

            # Diff v1 -> v2
            print("\n📊 Diff: Version 1 → Version 2")
            print("-" * 70)
            diff_v1_v2 = time_travel.diff_operations(path, op_v1, op_v2)
            print(f"Content changed: {diff_v1_v2['content_changed']}")
            print(f"Size change: {diff_v1_v2['size_diff']:+d} bytes")
            print(f"v1 size: {diff_v1_v2['operation_1']['metadata']['size']} bytes")
            print(f"v2 size: {diff_v1_v2['operation_2']['metadata']['size']} bytes")
            print()

            # Show actual unified diff
            import difflib

            print("Unified Diff (like git diff):")
            print("-" * 70)
            text_v1 = diff_v1_v2["operation_1"]["content"].decode("utf-8")
            text_v2 = diff_v1_v2["operation_2"]["content"].decode("utf-8")

            diff_lines = difflib.unified_diff(
                text_v1.splitlines(keepends=True),
                text_v2.splitlines(keepends=True),
                fromfile="Version 1",
                tofile="Version 2",
                lineterm="",
            )

            for line in diff_lines:
                line = line.rstrip()
                if line.startswith("---") or line.startswith("+++"):
                    print(f"\033[1m{line}\033[0m")  # Bold
                elif line.startswith("+"):
                    print(f"\033[92m{line}\033[0m")  # Green
                elif line.startswith("-"):
                    print(f"\033[91m{line}\033[0m")  # Red
                elif line.startswith("@@"):
                    print(f"\033[96m{line}\033[0m")  # Cyan
                else:
                    print(f"\033[2m{line}\033[0m")  # Dim
            print()

            # Diff v2 -> v3
            print("📊 Diff: Version 2 → Version 3")
            print("-" * 70)
            diff_v2_v3 = time_travel.diff_operations(path, op_v2, op_v3)
            print(f"Content changed: {diff_v2_v3['content_changed']}")
            print(f"Size change: {diff_v2_v3['size_diff']:+d} bytes")
            print()

            # Demonstrate directory listing at historical point
            print("=" * 70)
            print("4. Directory Time-Travel - What Files Existed When?")
            print("=" * 70)

            # Get operation after first file but before second
            print("\n📁 Directory listing at v1 (only agent_log.txt exists):")
            files_v1 = time_travel.list_files_at_operation("/workspace", op_v1)
            for file in files_v1:
                print(f"  {file['path']} ({file['size']} bytes)")

            print("\n📁 Directory listing at v3 (both files exist):")
            files_v3 = time_travel.list_files_at_operation("/workspace", op_v3)
            for file in files_v3:
                print(f"  {file['path']} ({file['size']} bytes)")
            print()

        nx.close()

        print("=" * 70)
        print("CLI Usage Examples")
        print("=" * 70)
        print()
        print("Time-travel features are available via CLI:")
        print()
        print("# Read file at historical operation:")
        print(f"  nexus cat /workspace/file.txt --at-operation {op_v1[:8]}")
        print()
        print("# List directory at historical operation:")
        print(f"  nexus ls /workspace --at-operation {op_v1[:8]}")
        print()
        print("# Diff file between two operations:")
        print(f"  nexus ops diff /workspace/file.txt {op_v1[:8]} {op_v2[:8]}")
        print("    → Shows metadata: size changes, timestamps, etc.")
        print()
        print(f"  nexus ops diff /workspace/file.txt {op_v1[:8]} {op_v2[:8]} --show-content")
        print("    → Shows full unified diff (like git diff) with line-by-line changes")
        print()
        print("# View operation log:")
        print("  nexus ops log")
        print("  nexus ops log --path /workspace/file.txt")
        print("  nexus ops log --agent my-agent")
        print()

        print("=" * 70)
        print("Use Cases")
        print("=" * 70)
        print()
        print("1. Debug agent behavior:")
        print("   - What was the file content 10 operations ago?")
        print("   - When did the agent change this value?")
        print("   - What files existed at that point in time?")
        print()
        print("2. Understand workflow evolution:")
        print("   - Track how agents modify files over time")
        print("   - Compare states at different operation points")
        print("   - Visualize concurrent agent operations")
        print()
        print("3. Non-destructive history exploration:")
        print("   - Explore past states without modifying current state")
        print("   - Analyze what happened without undo/redo")
        print("   - Perfect for post-mortem analysis")
        print()


if __name__ == "__main__":
    time_travel_demo()
