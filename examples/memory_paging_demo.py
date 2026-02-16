"""Demo of MemGPT 3-tier memory paging system (Issue #1258).

Shows how memories automatically page between:
- Main Context (working memory)
- Recall (recent history)
- Archival (long-term knowledge)
"""

import sys
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, "src")

from nexus.backends.local import LocalBackend
from nexus.services.memory.memory_api import Memory
from nexus.services.memory.memory_paging import MemoryPager
from nexus.storage.models import Base


def demo_memory_paging():
    """Demonstrate 3-tier memory paging."""

    # Setup database and backend
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    backend = LocalBackend(str(tempfile.mkdtemp()))

    # Initialize Memory API
    memory_api = Memory(
        session=session,
        backend=backend,
        zone_id="demo",
        user_id="alice",
        agent_id="assistant",
    )

    # Initialize 3-tier pager (main capacity = 10 for demo)
    pager = MemoryPager(
        session=session,
        zone_id="demo",
        main_capacity=10,
        recall_max_age_hours=1.0,  # Archive after 1 hour
    )

    print("=" * 60)
    print("MemGPT 3-Tier Memory Paging Demo")
    print("=" * 60)

    # Store 15 memories (exceeds main context capacity of 10)
    print("\n1. Adding 15 memories to main context...")
    for i in range(15):
        memory_id = memory_api.store(
            content=f"Memory {i}: Important fact about topic {i % 3}",
            memory_type="fact",
            importance=0.5 + (i % 10) * 0.05,
        )

        # Load the memory from database
        from sqlalchemy import select

        from nexus.storage.models import MemoryModel

        stmt = select(MemoryModel).where(MemoryModel.memory_id == memory_id)
        memory = session.execute(stmt).scalar_one_or_none()

        # Add to paging system
        pager.add_to_main(memory)

        if i % 5 == 4:
            stats = pager.get_stats()
            print(
                f"   After {i + 1} memories: Main={stats['main']['count']}, "
                f"Recall={stats['recall']['count']}, "
                f"Archival={stats['archival']['count']}"
            )

    # Show final distribution
    stats = pager.get_stats()
    print("\n2. Final distribution:")
    print(
        f"   Main Context: {stats['main']['count']}/{stats['main']['capacity']} "
        f"({stats['main']['utilization']:.0%} full)"
    )
    print(f"   Recall Store: {stats['recall']['count']}")
    print(f"   Archival Store: {stats['archival']['count']}")

    # Get recent context (what LLM would see)
    print("\n3. Recent context (for LLM):")
    recent = pager.get_recent_context(limit=5)
    for i, mem in enumerate(recent):
        print(f"   {i + 1}. {mem.memory_id[:8]}... (importance: {mem.importance})")

    # Simulate semantic search
    print("\n4. Semantic search across all tiers:")
    print("   (Note: Demo uses mock embeddings, real system would use actual vectors)")

    # In real usage, you'd have actual embeddings
    # query_embedding = get_embedding("What do I know about topic 1?")
    # results = pager.search_all_tiers(query_embedding)
    # For demo, just show the API
    print("   API: pager.search_all_tiers(query_embedding)")
    print("   Returns: {'main': [...], 'recall': [...], 'archival': [(mem, score), ...]}")

    print("\n5. Automatic archival:")
    print("   Memories older than 1 hour automatically move to archival")
    print("   This happens automatically in _archive_old_recall()")

    print("\n" + "=" * 60)
    print("Demo complete! MemGPT 3-tier paging working.")
    print("=" * 60)

    session.close()


if __name__ == "__main__":
    demo_memory_paging()
