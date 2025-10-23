"""Operation Log Demo - Undo & Audit Trail.

This demo shows the operation logging system with undo capability:
- All filesystem operations are logged automatically
- CAS-backed snapshots for undo
- Query operation history with filters
- Undo last operation
- Audit trail for compliance
"""

import tempfile
from pathlib import Path

import nexus
from nexus.storage.operation_logger import OperationLogger


def main() -> None:
    """Run the operation log demo."""
    print("=" * 70)
    print("Nexus Operation Log Demo - Undo & Audit Trail")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir) / "nexus-data"

        print(f"\n📁 Data directory: {data_dir}")

        # Initialize Nexus with agent context
        print("\n1. Connecting to Nexus...")
        nx = nexus.connect(config={"data_dir": str(data_dir), "agent_id": "demo-agent"})
        print("   ✓ Connected with agent_id: demo-agent")

        # ============================================================
        # Part 1: Automatic Operation Logging
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 1: Automatic Operation Logging")
        print("=" * 70)

        print("\n2. Writing files (operations logged automatically)...")
        nx.write("/workspace/version1.txt", b"Version 1 content")
        print("   ✓ Wrote version1.txt")

        nx.write("/workspace/version2.txt", b"Version 2 content")
        print("   ✓ Wrote version2.txt")

        # Update file (logs previous version)
        nx.write("/workspace/version1.txt", b"Version 1 UPDATED")
        print("   ✓ Updated version1.txt (previous version logged)")

        # Rename file
        nx.rename("/workspace/version2.txt", "/workspace/renamed.txt")
        print("   ✓ Renamed version2.txt to renamed.txt")

        # Delete file
        nx.delete("/workspace/renamed.txt")
        print("   ✓ Deleted renamed.txt (content snapshot saved)")

        # ============================================================
        # Part 2: Query Operation History
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 2: Query Operation History")
        print("=" * 70)

        print("\n3. Viewing operation log...")
        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # List all operations
            operations = logger.list_operations(limit=10)
            print(f"\n   📋 Total operations logged: {len(operations)}")
            print("\n   Recent operations:")
            for i, op in enumerate(operations[:5], 1):
                print(
                    f"   {i}. {op.operation_type:8} | {op.path:30} | {op.created_at.strftime('%H:%M:%S')}"
                )

            # Filter by operation type
            write_ops = logger.list_operations(operation_type="write", limit=10)
            print(f"\n   ✍️  Write operations: {len(write_ops)}")

            delete_ops = logger.list_operations(operation_type="delete", limit=10)
            print(f"   🗑️  Delete operations: {len(delete_ops)}")

            rename_ops = logger.list_operations(operation_type="rename", limit=10)
            print(f"   ✏️  Rename operations: {len(rename_ops)}")

            # Filter by agent
            agent_ops = logger.list_operations(agent_id="demo-agent", limit=10)
            print(f"\n   👤 Operations by demo-agent: {len(agent_ops)}")

        # ============================================================
        # Part 3: Undo Operations
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 3: Undo Operations")
        print("=" * 70)

        print("\n4. Demonstrating undo capability...")

        # Get last operation
        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)
            last_op = logger.get_last_operation(status="success")

            print("\n   Last operation:")
            print(f"   Type: {last_op.operation_type}")
            print(f"   Path: {last_op.path}")
            if last_op.new_path:
                print(f"   New Path: {last_op.new_path}")
            print(f"   Time: {last_op.created_at.strftime('%Y-%m-%d %H:%M:%S')}")

            # Undo based on operation type
            print(f"\n   🔄 Undoing {last_op.operation_type} operation...")

            if last_op.operation_type == "delete":
                # Restore deleted file
                if last_op.snapshot_hash:
                    content = nx.backend.read_content(last_op.snapshot_hash)
                    nx.write(last_op.path, content)
                    print(f"   ✓ Restored deleted file: {last_op.path}")

            elif last_op.operation_type == "rename":
                # Rename back to original
                if last_op.new_path and nx.exists(last_op.new_path):
                    nx.rename(last_op.new_path, last_op.path)
                    print(f"   ✓ Renamed {last_op.new_path} back to {last_op.path}")

            elif last_op.operation_type == "write":
                # Restore previous version
                if last_op.snapshot_hash:
                    old_content = nx.backend.read_content(last_op.snapshot_hash)
                    nx.write(last_op.path, old_content)
                    print(f"   ✓ Restored previous version of {last_op.path}")
                else:
                    # Was a new file, delete it
                    if nx.exists(last_op.path):
                        nx.delete(last_op.path)
                        print(f"   ✓ Deleted newly created file: {last_op.path}")

        # ============================================================
        # Part 4: Audit Trail
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 4: Audit Trail")
        print("=" * 70)

        print("\n5. Viewing audit trail for compliance...")

        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)

            # Get path history
            print("\n   📜 Operation history for /workspace/version1.txt:")
            history = logger.get_path_history("/workspace/version1.txt", limit=10)
            for i, op in enumerate(history, 1):
                metadata = logger.get_metadata_snapshot(op)
                print(
                    f"   {i}. {op.operation_type:8} | v{metadata['version'] if metadata else '?'} | {op.created_at.strftime('%H:%M:%S')}"
                )

            # Show snapshot capabilities
            if history and history[0].snapshot_hash:
                print(f"\n   💾 Snapshot available: {history[0].snapshot_hash[:16]}...")
                print("      Previous content can be restored from CAS")

        # ============================================================
        # Part 5: Key Features
        # ============================================================
        print("\n" + "=" * 70)
        print("PART 5: Key Features")
        print("=" * 70)

        print("\n✨ Operation Log Features:")
        print("   • Automatic logging of all operations (write, delete, rename)")
        print("   • CAS-backed snapshots (zero storage overhead)")
        print("   • Undo capability for any operation")
        print("   • Filter by agent, type, path, time, status")
        print("   • Complete audit trail for compliance")
        print("   • Query API for operation history")
        print("   • CLI commands: 'nexus ops log', 'nexus undo'")

        print("\n📊 Operation Statistics:")
        with nx.metadata.SessionLocal() as session:
            logger = OperationLogger(session)
            all_ops = logger.list_operations(limit=1000)
            write_ops = logger.list_operations(operation_type="write", limit=1000)
            delete_ops = logger.list_operations(operation_type="delete", limit=1000)
            rename_ops = logger.list_operations(operation_type="rename", limit=1000)

            print(f"   Total operations: {len(all_ops)}")
            print(f"   • Writes: {len(write_ops)}")
            print(f"   • Deletes: {len(delete_ops)}")
            print(f"   • Renames: {len(rename_ops)}")

        nx.close()

        print("\n" + "=" * 70)
        print("Demo Complete!")
        print("=" * 70)
        print("\nTry the CLI commands:")
        print("  nexus ops log --limit 20")
        print("  nexus ops log --type write")
        print("  nexus ops log --agent demo-agent")
        print("  nexus undo")
        print("=" * 70)


if __name__ == "__main__":
    main()
