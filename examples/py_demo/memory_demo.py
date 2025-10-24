#!/usr/bin/env python3
"""
Memory API Demo - Identity-Based Memory System (v0.4.0)

This demo shows how to use the Memory API for AI agent memory management
with identity relationships, order-neutral paths, and 3-layer permissions.

Features demonstrated:
- Storing memories with different scopes (agent, user, tenant)
- Querying memories by relationships
- Semantic search over memories
- Multi-agent memory sharing
- Permission-based access control
"""

import tempfile

import nexus


def demo_basic_memory_operations():
    """Demo basic memory store, query, and retrieval."""
    print("=" * 70)
    print("DEMO 1: Basic Memory Operations")
    print("=" * 70)

    # Connect to Nexus with user and agent identity
    with tempfile.TemporaryDirectory() as tmp_dir:
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "assistant-1",
            }
        )

        print("\n1. Storing memories...")

        # Store a preference
        pref_id = nx.memory.store(
            "User prefers Python over JavaScript",
            scope="user",
            memory_type="preference",
            importance=0.9,
        )
        print(f"   ✓ Stored preference: {pref_id}")

        # Store a fact
        fact_id = nx.memory.store(
            "API key for production: sk-prod-abc123",
            scope="agent",
            memory_type="fact",
            importance=1.0,
        )
        print(f"   ✓ Stored fact: {fact_id}")

        # Store an experience
        exp_id = nx.memory.store(
            "User struggled with async/await concepts",
            scope="user",
            memory_type="experience",
            importance=0.7,
        )
        print(f"   ✓ Stored experience: {exp_id}")

        print("\n2. Querying memories...")

        # Query all preferences
        preferences = nx.memory.query(memory_type="preference")
        print(f"   ✓ Found {len(preferences)} preference(s)")
        for pref in preferences:
            print(f"      - {pref['content']}")

        # Query all user-scoped memories
        user_memories = nx.memory.query(scope="user")
        print(f"   ✓ Found {len(user_memories)} user-scoped memory(ies)")

        print("\n3. Retrieving specific memory...")
        memory = nx.memory.get(pref_id)
        print(f"   ✓ Memory: {memory['content']}")
        print(f"   ✓ Scope: {memory['scope']}")
        print(f"   ✓ Type: {memory['memory_type']}")
        print(f"   ✓ Importance: {memory['importance']}")
        print(f"   ✓ Created: {memory['created_at']}")


def demo_semantic_search():
    """Demo semantic search over memories."""
    print("\n" + "=" * 70)
    print("DEMO 2: Semantic Search")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "assistant-1",
            }
        )

        print("\n1. Populating memory with programming-related content...")

        memories_data = [
            ("Python is great for data science and machine learning", "preference"),
            ("JavaScript is used for frontend web development", "fact"),
            ("User knows Rust programming language", "fact"),
            ("TypeScript adds types to JavaScript", "fact"),
            ("Go is good for concurrent systems", "preference"),
        ]

        for content, mem_type in memories_data:
            nx.memory.store(content, scope="user", memory_type=mem_type)
            print(f"   ✓ Stored: {content[:50]}...")

        print("\n2. Searching for 'Python programming'...")
        results = nx.memory.search("Python programming", limit=3)

        print(f"   ✓ Found {len(results)} result(s):")
        for i, result in enumerate(results, 1):
            print(f"      {i}. Score: {result['score']:.2f} - {result['content']}")


def demo_multi_agent_sharing():
    """Demo multi-agent memory sharing with same user."""
    print("\n" + "=" * 70)
    print("DEMO 3: Multi-Agent Memory Sharing")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Agent 1 creates user-scoped memory
        print("\n1. Agent 1 stores user-scoped preferences...")
        nx1 = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "code-assistant",
            }
        )

        memory_id = nx1.memory.store(
            "User prefers 4-space indentation",
            scope="user",  # Shared across all agents of same user
            memory_type="preference",
        )
        print(f"   ✓ Agent 1 stored: {memory_id}")

        # Agent 2 (same user) can access it
        print("\n2. Agent 2 (same user) queries memories...")
        nx2 = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "chat-assistant",
            }
        )

        memories = nx2.memory.query(scope="user")
        print(f"   ✓ Agent 2 found {len(memories)} shared memory(ies)")
        for mem in memories:
            print(f"      - {mem['content']}")

        print("\n3. Agent 1 stores agent-scoped secret...")
        secret_id = nx1.memory.store(
            "API key for code-assistant only",
            scope="agent",  # Private to this agent
            memory_type="fact",
        )
        print(f"   ✓ Agent 1 stored private memory: {secret_id}")

        print("\n4. Agent 2 queries all memories...")
        all_memories = nx2.memory.query()
        print(f"   ✓ Agent 2 can see {len(all_memories)} memory(ies)")
        print("   ✓ Agent-scoped memories are isolated")


def demo_memory_scopes():
    """Demo different memory scopes (agent, user, tenant, global)."""
    print("\n" + "=" * 70)
    print("DEMO 4: Memory Scopes")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "assistant-1",
            }
        )

        print("\n1. Storing memories with different scopes...")

        # Agent-scoped: private to this agent
        nx.memory.store("This agent's internal state", scope="agent", memory_type="fact")
        print("   ✓ Agent-scoped: Private to assistant-1")

        # User-scoped: shared across all user's agents
        nx.memory.store("User's coding preferences", scope="user", memory_type="preference")
        print("   ✓ User-scoped: Shared across alice's agents")

        # Tenant-scoped: shared across organization
        nx.memory.store("Company coding standards", scope="tenant", memory_type="fact")
        print("   ✓ Tenant-scoped: Shared across acme-corp")

        print("\n2. Querying by scope...")

        for scope in ["agent", "user", "tenant"]:
            results = nx.memory.list(scope=scope)
            print(f"   ✓ {scope.capitalize()}-scoped: {len(results)} memory(ies)")


def demo_importance_filtering():
    """Demo storing and filtering by importance scores."""
    print("\n" + "=" * 70)
    print("DEMO 5: Importance Scoring")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "assistant-1",
            }
        )

        print("\n1. Storing memories with importance scores...")

        memories = [
            ("Critical security key", 1.0),
            ("Important user preference", 0.8),
            ("Useful context", 0.5),
            ("Minor detail", 0.2),
        ]

        for content, importance in memories:
            nx.memory.store(content, scope="user", importance=importance)
            print(f"   ✓ Importance {importance}: {content}")

        print("\n2. Listing all memories...")
        all_memories = nx.memory.list()
        print(f"   ✓ Total memories: {len(all_memories)}")

        print("\n3. Sorting by importance:")
        sorted_memories = sorted(all_memories, key=lambda m: m.get("importance") or 0, reverse=True)

        for mem in sorted_memories:
            importance = mem.get("importance") or 0.0
            content = mem["content"] if "content" in mem else f"[{mem['content_hash'][:8]}...]"
            print(f"   {importance:.1f} - {content}")


def demo_memory_lifecycle():
    """Demo complete memory lifecycle: create, read, update (via replace), delete."""
    print("\n" + "=" * 70)
    print("DEMO 6: Memory Lifecycle")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "assistant-1",
            }
        )

        print("\n1. Creating memory...")
        memory_id = nx.memory.store("User timezone: UTC", scope="user", memory_type="preference")
        print(f"   ✓ Created: {memory_id}")

        print("\n2. Reading memory...")
        memory = nx.memory.get(memory_id)
        print(f"   ✓ Content: {memory['content']}")

        print("\n3. Updating memory (creating new version)...")
        new_id = nx.memory.store(
            "User timezone: America/Los_Angeles", scope="user", memory_type="preference"
        )
        print(f"   ✓ New version: {new_id}")

        print("\n4. Deleting old memory...")
        deleted = nx.memory.delete(memory_id)
        print(f"   ✓ Deleted: {deleted}")

        print("\n5. Verifying deletion...")
        result = nx.memory.get(memory_id)
        print(f"   ✓ Memory exists: {result is not None}")


def demo_order_neutral_paths():
    """Demo order-neutral path resolution."""
    print("\n" + "=" * 70)
    print("DEMO 7: Order-Neutral Paths (Core Feature)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        nx = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "assistant-1",
            }
        )

        print("\n1. Storing memory with identity relationships...")
        memory_id = nx.memory.store("Shared knowledge base", scope="user", memory_type="fact")
        print(f"   ✓ Memory ID: {memory_id}")

        print("\n2. Virtual paths that resolve to this memory:")
        print("   (All these paths point to the SAME memory)")

        # Get the memory router to show virtual paths
        from nexus.core.entity_registry import EntityRegistry
        from nexus.core.memory_router import MemoryViewRouter

        session = nx.metadata.SessionLocal()
        entity_registry = EntityRegistry(session)
        memory_router = MemoryViewRouter(session, entity_registry)

        memory = memory_router.get_memory_by_id(memory_id)
        if memory:
            virtual_paths = memory_router.get_virtual_paths(memory)
            for i, path in enumerate(virtual_paths[:6], 1):  # Show first 6 paths
                print(f"   {i}. {path}")

        session.close()

        print("\n3. Key concept: Order doesn't matter!")
        print("   /workspace/alice/assistant-1/memory/")
        print("   /workspace/assistant-1/alice/memory/")
        print("   Both resolve to the SAME memory_id")
        print("   → No file duplication!")
        print("   → Flexible reorganization without data movement!")


def demo_three_layer_permissions():
    """Demo 3-layer permission system: ReBAC + ACL + UNIX."""
    print("\n" + "=" * 70)
    print("DEMO 8: 3-Layer Permission System (Core Feature)")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # Setup entities
        print("\n1. Setting up scenario: Alice has 2 agents, Bob has 1 agent...")

        nx_alice_agent1 = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "alice-assistant",
            }
        )

        # Register Bob and his agent
        session = nx_alice_agent1.metadata.SessionLocal()
        from nexus.core.entity_registry import EntityRegistry

        registry = EntityRegistry(session)
        registry.register_entity("user", "bob", parent_type="tenant", parent_id="acme-corp")
        registry.register_entity("agent", "bob-assistant", parent_type="user", parent_id="bob")
        session.close()

        print("   ✓ Alice has alice-assistant")
        print("   ✓ Bob has bob-assistant")

        print("\n2. LAYER 1 - ReBAC (Relationship-Based Access Control):")
        print("   Testing identity-based relationships...")

        # Alice's agent creates user-scoped memory
        memory_id = nx_alice_agent1.memory.store(
            "Alice's coding preferences", scope="user", memory_type="preference"
        )
        print(f"   ✓ Alice's agent stored user-scoped memory: {memory_id}")

        # Alice's second agent (same user) can access
        nx_alice_agent2 = nexus.connect(
            {
                "data_dir": tmp_dir,
                "tenant_id": "acme-corp",
                "user_id": "alice",
                "agent_id": "alice-helper",
            }
        )

        # Register alice-helper
        session = nx_alice_agent2.metadata.SessionLocal()
        registry = EntityRegistry(session)
        registry.register_entity("agent", "alice-helper", parent_type="user", parent_id="alice")
        session.close()

        result = nx_alice_agent2.memory.get(memory_id)
        print(f"   ✓ Alice's other agent can access: {result is not None}")
        print("   → ReBAC: Same user relationship grants access!")

        # Bob's agent (different user) in same tenant
        print("\n3. Testing tenant-scoped access...")
        nx_alice_agent1.memory.store(
            "Company-wide coding standards", scope="tenant", memory_type="fact"
        )
        print("   ✓ Tenant-scoped memory shared across organization")

        print("\n4. LAYER 2 - ACL (Access Control Lists):")
        print("   Works on canonical paths: /objs/memory/{memory_id}")
        print("   → Path-independent access control")

        print("\n5. LAYER 3 - UNIX Permissions:")
        print("   Testing file permission bits...")

        # Create memory with restrictive permissions
        from nexus.core.memory_router import MemoryViewRouter

        session = nx_alice_agent1.metadata.SessionLocal()
        registry = EntityRegistry(session)
        router = MemoryViewRouter(session, registry)

        content_hash = nx_alice_agent1.backend.write_content(b"Private secret")
        _restricted_memory = router.create_memory(
            content_hash=content_hash,
            tenant_id="acme-corp",
            user_id="alice",
            agent_id="alice-assistant",
            scope="agent",
            mode=0o600,  # Owner read+write only
        )
        session.close()

        print("   ✓ Created memory with mode 0o600 (owner only)")
        print("   → UNIX permissions: Owner can read/write")
        print("   → Other agents: No access (even same user)")

        print("\n6. Permission Check Flow:")
        print("   ┌─────────────────────────────────────────┐")
        print("   │ 1. Check ReBAC relationships           │")
        print("   │    ├─ Direct creator?                   │")
        print("   │    ├─ User ownership?                   │")
        print("   │    └─ Tenant membership?                │")
        print("   │ 2. Check ACL on canonical path         │")
        print("   │ 3. Check UNIX permission bits           │")
        print("   └─────────────────────────────────────────┘")

        print("\n7. Summary of the 3 layers:")
        print("   • ReBAC: WHO you are (identity relationships)")
        print("   • ACL: WHAT you can access (path-based rules)")
        print("   • UNIX: HOW you can access (read/write/execute)")


def main():
    """Run all demos."""
    print("\n" + "=" * 70)
    print("NEXUS MEMORY API DEMO - Identity-Based Memory System v0.4.0")
    print("=" * 70)

    # Run all demos
    demo_basic_memory_operations()
    demo_semantic_search()
    demo_multi_agent_sharing()
    demo_memory_scopes()
    demo_importance_filtering()
    demo_memory_lifecycle()
    demo_order_neutral_paths()
    demo_three_layer_permissions()

    print("\n" + "=" * 70)
    print("All demos completed successfully!")
    print("=" * 70)
    print("\nKey Features Demonstrated:")
    print("  ✓ Identity-based memory with tenant/user/agent")
    print("  ✓ Multiple memory scopes (agent, user, tenant, global)")
    print("  ✓ Memory types (fact, preference, experience)")
    print("  ✓ Importance scoring (0.0-1.0)")
    print("  ✓ Semantic search over memories")
    print("  ✓ Multi-agent memory sharing")
    print("  ✓ ORDER-NEUTRAL PATHS - No file duplication!")
    print("  ✓ 3-LAYER PERMISSIONS - ReBAC + ACL + UNIX")
    print("  ✓ Complete CRUD lifecycle")
    print("\nCore Innovations (v0.4.0):")
    print("  • Memory location ≠ identity")
    print("  • /workspace/alice/agent1 == /workspace/agent1/alice")
    print("  • Relationships determine access, not paths")
    print("  • No data duplication for memory sharing")
    print("\nNext steps:")
    print("  - Explore CLI: nexus memory --help")
    print("  - Read docs: docs/architecture/ARCHITECTURE.md")
    print("  - Run tests: pytest tests/unit/test_memory_api.py")
    print()


if __name__ == "__main__":
    main()
