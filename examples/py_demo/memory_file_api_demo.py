#!/usr/bin/env python3
"""Demo: Phase 2 Integration - Memory Paths with File API (v0.4.0)

This demo shows how memory virtual paths work with normal file operations.
Users can choose between Memory API or File API - both access the same memories!
"""

import tempfile

import nexus


def demo_file_api_integration():
    """Demonstrate Phase 2 Integration: Memory paths with File API."""

    print("=" * 70)
    print("Phase 2 Integration: Memory Paths with File API (v0.4.0)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Connect with full identity context
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme",
                "user_id": "alice",
                "agent_id": "agent1",
            }
        )

        # =====================================================================
        # DEMO 1: Order-Neutral Paths with File API
        # =====================================================================
        print("\n" + "─" * 70)
        print("DEMO 1: Order-Neutral Paths with File API")
        print("─" * 70)
        print("\nConcept: Multiple path orders access the SAME memory!\n")

        # Store via one path
        result = nx.write("/workspace/alice/agent1/memory/facts", b"Python is great!")
        print("✓ Stored via: /workspace/alice/agent1/memory/facts")
        print(f"  Content hash: {result['etag'][:16]}...")

        # Read via different path orders - ALL access the same memory!
        paths = [
            "/workspace/alice/agent1/memory/facts",
            "/workspace/agent1/alice/memory/facts",  # Different order!
            "/memory/by-user/alice/facts",  # User-centric view
            "/memory/by-agent/agent1/facts",  # Agent-centric view
        ]

        print("\n✓ Reading from different path orders (all return same content):")
        for path in paths:
            try:
                content = nx.read(path)
                print(f"  {path:<50} → {content.decode()}")
            except Exception as e:
                print(f"  {path:<50} → Error: {e}")

        # =====================================================================
        # DEMO 2: File API vs Memory API - Two Ways, Same Result
        # =====================================================================
        print("\n" + "─" * 70)
        print("DEMO 2: File API vs Memory API - Two Ways, Same Result")
        print("─" * 70)

        # Method 1: Memory API (traditional)
        print("\nMethod 1: Memory API")
        mem_id = nx.memory.store("Machine learning is awesome!", scope="user")
        print(f"  ✓ nx.memory.store() → memory_id: {mem_id}")
        mem = nx.memory.get(mem_id)
        print(f"  ✓ nx.memory.get()   → content: {mem['content']}")

        # Method 2: File API (NEW in Phase 2!)
        print("\nMethod 2: File API (Phase 2 Integration)")
        nx.write("/workspace/alice/agent1/memory/preferences", b"I love Python!")
        print("  ✓ nx.write() → /workspace/alice/agent1/memory/preferences")
        content = nx.read("/workspace/alice/agent1/memory/preferences")
        print(f"  ✓ nx.read()  → content: {content.decode()}")

        print("\n💡 Both methods store memories in the same system!")
        print("   Users can choose their preferred interface.")

        # =====================================================================
        # DEMO 3: Directory Listing for Memories
        # =====================================================================
        print("\n" + "─" * 70)
        print("DEMO 3: Directory Listing for Memories")
        print("─" * 70)

        # List memories via file API
        memories = nx.list("/workspace/alice/agent1/memory")
        print(f"\n✓ nx.list('/workspace/alice/agent1/memory') found {len(memories)} memories:")
        for path in memories[:5]:
            content = nx.read(path)
            preview = content.decode()[:40] + "..." if len(content) > 40 else content.decode()
            print(f"  • {path}")
            print(f"    Preview: {preview}")

        # =====================================================================
        # DEMO 4: CRUD Operations with File API
        # =====================================================================
        print("\n" + "─" * 70)
        print("DEMO 4: CRUD Operations with File API")
        print("─" * 70)

        # Create
        print("\n1. Create:")
        result = nx.write("/workspace/alice/agent1/memory/todo", b"Buy groceries")
        print("   ✓ Created memory via nx.write()")
        print("     Path: /workspace/alice/agent1/memory/todo")
        print(f"     Hash: {result['etag'][:16]}...")

        # Read
        print("\n2. Read:")
        content = nx.read("/workspace/alice/agent1/memory/todo")
        print("   ✓ Read memory via nx.read()")
        print(f"     Content: {content.decode()}")

        # Update (overwrite)
        print("\n3. Update:")
        nx.write("/workspace/alice/agent1/memory/todo", b"Buy groceries and cook dinner")
        updated = nx.read("/workspace/alice/agent1/memory/todo")
        print("   ✓ Updated memory via nx.write()")
        print(f"     New content: {updated.decode()}")

        # Delete
        print("\n4. Delete:")
        # Get memory ID from canonical path
        from nexus.core.entity_registry import EntityRegistry
        from nexus.core.memory_router import MemoryViewRouter

        session = nx.metadata.SessionLocal()
        router = MemoryViewRouter(session, EntityRegistry(session))
        memory = router.resolve("/workspace/alice/agent1/memory/todo")
        session.close()

        if memory:
            nx.delete(f"/objs/memory/{memory.memory_id}")
            print("   ✓ Deleted memory via nx.delete()")
            print(f"     Memory ID: {memory.memory_id}")

        # =====================================================================
        # DEMO 5: Canonical Paths (Direct Access)
        # =====================================================================
        print("\n" + "─" * 70)
        print("DEMO 5: Canonical Paths (Direct Access)")
        print("─" * 70)

        # Store and get canonical path
        print("\nStoring memory and getting all access paths:")
        mem_id = nx.memory.store("Deep learning breakthrough!", scope="user")

        session = nx.metadata.SessionLocal()
        router = MemoryViewRouter(session, EntityRegistry(session))
        memory = router.get_memory_by_id(mem_id)
        virtual_paths = router.get_virtual_paths(memory)
        session.close()

        print(f"\n✓ Memory ID: {mem_id}")
        print(f"✓ All valid access paths ({len(virtual_paths)} total):")
        for i, path in enumerate(virtual_paths[:8], 1):
            print(f"   {i}. {path}")
        if len(virtual_paths) > 8:
            print(f"   ... and {len(virtual_paths) - 8} more")

        # Access via canonical path
        print("\n✓ Reading via canonical path:")
        canonical = f"/objs/memory/{mem_id}"
        content = nx.read(canonical)
        print(f"   Path: {canonical}")
        print(f"   Content: {content.decode()}")

        # =====================================================================
        # DEMO 6: Mixing File API and Memory API
        # =====================================================================
        print("\n" + "─" * 70)
        print("DEMO 6: Mixing File API and Memory API")
        print("─" * 70)

        print("\n1. Store via File API:")
        nx.write("/workspace/alice/agent1/memory/research", b"Transformers paper")
        print("   ✓ nx.write('/workspace/alice/agent1/memory/research', ...)")

        print("\n2. Query via Memory API:")
        memories = nx.memory.query(user_id="alice", scope="user", limit=3)
        print(f"   ✓ nx.memory.query(user_id='alice') → {len(memories)} memories")

        print("\n3. Read via File API:")
        content = nx.read("/workspace/alice/agent1/memory/research")
        print("   ✓ nx.read('/workspace/alice/agent1/memory/research')")
        print(f"     → {content.decode()}")

        print("\n💡 Both APIs access the same underlying memory system!")

        # =====================================================================
        # Summary
        # =====================================================================
        print("\n" + "=" * 70)
        print("Summary: Phase 2 Integration Benefits")
        print("=" * 70)
        print("""
✓ Order-Neutral Paths: Any ID order works
  /workspace/alice/agent1/memory → Same as → /workspace/agent1/alice/memory

✓ Two APIs, One System: Choose your interface
  - Memory API: nx.memory.store() / get() / query()
  - File API: nx.read() / write() / delete()

✓ Virtual Paths: Multiple views of same memory
  - /objs/memory/{id} (canonical)
  - /workspace/{user}/{agent}/memory (workspace view)
  - /memory/by-user/{user} (user-centric)
  - /memory/by-agent/{agent} (agent-centric)

✓ CLI Support: Works with nexus cat/write/ls/rm commands

✓ Forward Compatible: Ready for issue #121 workspace structure
        """)


if __name__ == "__main__":
    demo_file_api_integration()
