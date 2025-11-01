"""Memory consolidation engine for importance-based merging."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from nexus.llm.message import Message, MessageRole
from nexus.llm.provider import LLMProvider
from nexus.storage.models import MemoryModel


class ConsolidationEngine:
    """Consolidate memories based on importance and similarity.

    Implements intelligent memory consolidation to prevent context overflow
    by merging related low-importance memories into high-importance summaries.
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        llm_provider: LLMProvider,
        user_id: str,
        agent_id: str | None = None,
        tenant_id: str | None = None,
    ):
        """Initialize consolidation engine.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            llm_provider: LLM provider for consolidation
            user_id: User ID for ownership
            agent_id: Optional agent ID
            tenant_id: Optional tenant ID
        """
        self.session = session
        self.backend = backend
        self.llm_provider = llm_provider
        self.user_id = user_id
        self.agent_id = agent_id
        self.tenant_id = tenant_id

    async def consolidate_async(
        self,
        memory_ids: list[str],
        importance_threshold: float = 0.5,
        max_consolidated_memories: int = 10,
    ) -> dict[str, Any]:
        """Consolidate multiple memories into a summary (async).

        Args:
            memory_ids: List of memory IDs to consolidate
            importance_threshold: Only consolidate memories below this importance
            max_consolidated_memories: Maximum memories to include in one consolidation

        Returns:
            Dictionary with consolidation results:
                - consolidated_memory_id: ID of new consolidated memory
                - source_memory_ids: List of source memory IDs
                - memories_consolidated: Number of memories consolidated
                - importance_score: Importance of consolidated memory

        Example:
            >>> result = await consolidation_engine.consolidate_async(
            ...     memory_ids=["mem_1", "mem_2", "mem_3"],
            ...     importance_threshold=0.6
            ... )
            >>> print(f"Consolidated {result['memories_consolidated']} memories")
        """
        # Load memories
        memories = []
        for memory_id in memory_ids[:max_consolidated_memories]:
            memory_data = self._load_memory(memory_id)
            if memory_data and memory_data.get("importance", 0.0) < importance_threshold:
                memories.append(memory_data)

        if len(memories) < 2:
            raise ValueError("Need at least 2 memories to consolidate")

        # Build consolidation prompt
        prompt = self._build_consolidation_prompt(memories)

        # Call LLM for consolidation
        messages = [Message(role=MessageRole.USER, content=prompt)]
        response = await self.llm_provider.complete_async(messages)
        consolidated_text = response.content

        # Calculate importance (max of source memories + bonus)
        max_importance = max(m.get("importance", 0.0) for m in memories)
        consolidated_importance = min(max_importance + 0.1, 1.0)

        # Store consolidated memory
        consolidated_memory_id = self._store_consolidated_memory(
            memories,
            consolidated_text or "",
            consolidated_importance,
        )

        # Mark source memories as consolidated
        self._mark_memories_consolidated(
            [m["memory_id"] for m in memories],
            consolidated_memory_id,
        )

        return {
            "consolidated_memory_id": consolidated_memory_id,
            "source_memory_ids": [m["memory_id"] for m in memories],
            "memories_consolidated": len(memories),
            "importance_score": consolidated_importance,
        }

    def consolidate_by_criteria(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        namespace: str | None = None,  # v0.8.0: Exact namespace
        namespace_prefix: str | None = None,  # v0.8.0: Namespace prefix
        importance_max: float = 0.5,
        batch_size: int = 10,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Consolidate memories matching criteria.

        Args:
            memory_type: Filter by memory type
            scope: Filter by scope
            namespace: Filter by exact namespace match. v0.8.0
            namespace_prefix: Filter by namespace prefix. v0.8.0
            importance_max: Only consolidate memories with importance <= this
            batch_size: Number of memories to consolidate per batch
            limit: Maximum total memories to process

        Returns:
            List of consolidation results
        """
        import asyncio

        # Query candidate memories
        query = self.session.query(MemoryModel).filter(
            MemoryModel.agent_id == self.agent_id,
            MemoryModel.importance <= importance_max,
            MemoryModel.consolidated_from.is_(None),  # Not already consolidated
        )

        if memory_type:
            query = query.filter_by(memory_type=memory_type)
        if scope:
            query = query.filter_by(scope=scope)

        # v0.8.0: Namespace filtering
        if namespace:
            query = query.filter_by(namespace=namespace)
        elif namespace_prefix:
            query = query.filter(MemoryModel.namespace.like(f"{namespace_prefix}%"))

        query = query.order_by(MemoryModel.created_at.desc()).limit(limit)
        memories = query.all()

        if len(memories) < 2:
            return []

        # Group into batches
        results = []
        for i in range(0, len(memories), batch_size):
            batch = memories[i : i + batch_size]
            if len(batch) < 2:
                continue

            memory_ids = [m.memory_id for m in batch]

            try:
                result = asyncio.run(self.consolidate_async(memory_ids, importance_max))
                results.append(result)
            except Exception as e:
                # Log error but continue with other batches
                print(f"Consolidation batch failed: {e}")
                continue

        return results

    def _load_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Load memory with content.

        Args:
            memory_id: Memory ID

        Returns:
            Memory data dictionary or None if not found
        """
        memory = self.session.query(MemoryModel).filter_by(memory_id=memory_id).first()
        if not memory:
            return None

        try:
            content_bytes = self.backend.read_content(memory.content_hash)
            content = content_bytes.decode("utf-8")
        except Exception:
            content = ""

        return {
            "memory_id": memory.memory_id,
            "content": content,
            "memory_type": memory.memory_type,
            "importance": memory.importance or 0.0,
            "scope": memory.scope,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
        }

    def _build_consolidation_prompt(self, memories: list[dict[str, Any]]) -> str:
        """Build consolidation prompt for LLM.

        Args:
            memories: List of memory dictionaries

        Returns:
            Consolidation prompt
        """
        prompt = """# Memory Consolidation Task

You are consolidating multiple related memories into a concise, high-value summary.

## Source Memories

"""

        for i, memory in enumerate(memories, 1):
            content = memory.get("content", "")
            importance = memory.get("importance", 0.0)
            mem_type = memory.get("memory_type", "unknown")

            prompt += f"""### Memory {i} (Type: {mem_type}, Importance: {importance:.2f})
{content}

"""

        prompt += """## Your Task

Create a consolidated summary that:
1. Captures the essential information from all source memories
2. Removes redundancy while preserving unique insights
3. Maintains factual accuracy
4. Is concise yet comprehensive

Provide only the consolidated summary, no additional commentary.
"""

        return prompt

    def _store_consolidated_memory(
        self,
        source_memories: list[dict[str, Any]],
        consolidated_content: str,
        importance: float,
    ) -> str:
        """Store consolidated memory.

        Args:
            source_memories: List of source memory dictionaries
            consolidated_content: Consolidated content text
            importance: Importance score

        Returns:
            memory_id: ID of consolidated memory
        """
        memory_id = str(uuid.uuid4())

        # Prepare consolidated content with metadata
        content_data = {
            "type": "consolidated",
            "content": consolidated_content,
            "source_count": len(source_memories),
            "consolidated_at": datetime.now(UTC).isoformat(),
        }

        # Store in CAS
        content_json = json.dumps(content_data, indent=2).encode("utf-8")
        content_hash = self.backend.write_content(content_json)

        # Track source memory IDs
        source_ids = [m["memory_id"] for m in source_memories]

        # Create memory record
        memory = MemoryModel(
            memory_id=memory_id,
            content_hash=content_hash,
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            agent_id=self.agent_id,
            scope="agent",
            visibility="private",
            memory_type="consolidated",
            importance=importance,
            consolidated_from=json.dumps(source_ids),
            consolidation_version=1,
        )

        self.session.add(memory)
        self.session.commit()

        return memory_id

    def _mark_memories_consolidated(
        self,
        memory_ids: list[str],
        _consolidated_memory_id: str,
    ) -> None:
        """Mark source memories as consolidated.

        Args:
            memory_ids: List of source memory IDs
            _consolidated_memory_id: ID of consolidated memory

        Note:
            This doesn't delete source memories, just marks them as consolidated.
            They can be cleaned up later by a garbage collection process.
        """
        # Update source memories to track consolidation
        for memory_id in memory_ids:
            memory = self.session.query(MemoryModel).filter_by(memory_id=memory_id).first()
            if memory:
                # Track consolidation (could add a consolidated_into field in future)
                memory.importance = max(memory.importance or 0.0, 0.1)  # Lower importance
                # Could also add: memory.consolidated_into = _consolidated_memory_id

        self.session.commit()

    def sync_consolidate(
        self,
        memory_ids: list[str],
        importance_threshold: float = 0.5,
        max_consolidated_memories: int = 10,
    ) -> dict[str, Any]:
        """Synchronous wrapper for consolidate_async.

        Args:
            memory_ids: List of memory IDs to consolidate
            importance_threshold: Only consolidate memories below this importance
            max_consolidated_memories: Maximum memories to include

        Returns:
            Consolidation results
        """
        import asyncio

        return asyncio.run(
            self.consolidate_async(memory_ids, importance_threshold, max_consolidated_memories)
        )
