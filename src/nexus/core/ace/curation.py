"""Curation system for updating playbooks with reflection learnings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from nexus.core.ace.playbook import PlaybookManager
from nexus.storage.models import MemoryModel


class Curator:
    """Curate playbooks by integrating reflection learnings.

    Takes reflection memories and updates playbooks with:
    - New helpful strategies (✓)
    - New harmful patterns to avoid (✗)
    - Neutral observations (○)
    """

    def __init__(
        self,
        session: Session,
        backend: Any,
        playbook_manager: PlaybookManager,
    ):
        """Initialize curator.

        Args:
            session: Database session
            backend: Storage backend for CAS content
            playbook_manager: Playbook manager for updating playbooks
        """
        self.session = session
        self.backend = backend
        self.playbook_manager = playbook_manager

    def curate_playbook(
        self,
        playbook_id: str,
        reflection_memory_ids: list[str],
        merge_threshold: float = 0.7,
    ) -> dict[str, Any]:
        """Update playbook with reflection learnings.

        Args:
            playbook_id: Playbook ID to update
            reflection_memory_ids: List of reflection memory IDs
            merge_threshold: Similarity threshold for merging similar strategies (0.0-1.0)

        Returns:
            Dictionary with curation results:
                - strategies_added: Number of new strategies added
                - strategies_merged: Number of strategies merged
                - strategies_total: Total strategies after curation

        Example:
            >>> result = curator.curate_playbook(
            ...     playbook_id,
            ...     reflection_memory_ids=["mem_123", "mem_456"]
            ... )
            >>> print(f"Added {result['strategies_added']} new strategies")
        """
        # Get current playbook
        playbook_data = self.playbook_manager.get_playbook(playbook_id)
        if not playbook_data:
            raise ValueError(f"Playbook {playbook_id} not found")

        existing_strategies = playbook_data["content"].get("strategies", [])

        # Collect reflection data
        new_strategies = []
        for memory_id in reflection_memory_ids:
            reflection_data = self._load_reflection_memory(memory_id)
            if reflection_data:
                new_strategies.extend(
                    self._extract_strategies_from_reflection(reflection_data, memory_id)
                )

        # Merge with existing strategies
        merged_strategies, stats = self._merge_strategies(
            existing_strategies,
            new_strategies,
            merge_threshold,
        )

        # Update playbook
        self.playbook_manager.update_playbook(
            playbook_id,
            strategies=merged_strategies if stats["strategies_added"] > 0 else None,
            metadata={
                "last_curation": datetime.now(UTC).isoformat(),
                "curation_stats": stats,
            },
            increment_version=True,
        )

        return {
            "playbook_id": playbook_id,
            "strategies_added": stats["strategies_added"],
            "strategies_merged": stats["strategies_merged"],
            "strategies_total": len(existing_strategies) + stats["strategies_added"],
        }

    def curate_from_trajectory(
        self,
        playbook_id: str,
        trajectory_id: str,
    ) -> dict[str, Any] | None:
        """Curate playbook from a trajectory's reflection memories.

        Args:
            playbook_id: Playbook ID to update
            trajectory_id: Trajectory ID to pull reflections from

        Returns:
            Curation results or None if no reflections found
        """
        # Find reflection memories for this trajectory
        reflection_memories = (
            self.session.query(MemoryModel)
            .filter_by(
                trajectory_id=trajectory_id,
                memory_type="reflection",
            )
            .all()
        )

        if not reflection_memories:
            return None

        memory_ids = [m.memory_id for m in reflection_memories]
        return self.curate_playbook(playbook_id, memory_ids)

    def _load_reflection_memory(self, memory_id: str) -> dict[str, Any] | None:
        """Load reflection memory content.

        Args:
            memory_id: Memory ID

        Returns:
            Reflection data or None if not found
        """
        memory = self.session.query(MemoryModel).filter_by(memory_id=memory_id).first()
        if not memory:
            return None

        try:
            content_bytes = self.backend.read_content(memory.content_hash)
            content_data: dict[str, Any] = json.loads(content_bytes.decode("utf-8"))
            return content_data
        except Exception:
            return None

    def _extract_strategies_from_reflection(
        self,
        reflection_data: dict[str, Any],
        memory_id: str,
    ) -> list[dict[str, Any]]:
        """Extract strategies from reflection data.

        Args:
            reflection_data: Reflection content
            memory_id: Memory ID for evidence tracking

        Returns:
            List of strategy dictionaries
        """
        strategies = []
        reflection = reflection_data.get("reflection", {})
        trajectory_id = reflection_data.get("trajectory_id")

        # Extract helpful strategies
        for item in reflection.get("helpful_strategies", []):
            strategies.append(
                {
                    "type": "helpful",
                    "description": item.get("description", ""),
                    "evidence": item.get("evidence", ""),
                    "confidence": item.get("confidence", 0.5),
                    "source_trajectory": trajectory_id,
                    "source_memory": memory_id,
                    "added_at": datetime.now(UTC).isoformat(),
                }
            )

        # Extract harmful patterns
        for item in reflection.get("harmful_patterns", []):
            strategies.append(
                {
                    "type": "harmful",
                    "description": item.get("description", ""),
                    "evidence": item.get("evidence", ""),
                    "impact": item.get("impact", ""),
                    "confidence": item.get("confidence", 0.5),
                    "source_trajectory": trajectory_id,
                    "source_memory": memory_id,
                    "added_at": datetime.now(UTC).isoformat(),
                }
            )

        # Extract neutral observations
        for item in reflection.get("observations", []):
            strategies.append(
                {
                    "type": "neutral",
                    "description": item.get("description", ""),
                    "relevance": item.get("relevance", ""),
                    "confidence": 0.5,
                    "source_trajectory": trajectory_id,
                    "source_memory": memory_id,
                    "added_at": datetime.now(UTC).isoformat(),
                }
            )

        return strategies

    def _merge_strategies(
        self,
        existing: list[dict[str, Any]],
        new: list[dict[str, Any]],
        similarity_threshold: float,
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Merge new strategies with existing ones.

        Args:
            existing: Existing strategies
            new: New strategies to add
            similarity_threshold: Threshold for considering strategies similar (0.0-1.0)

        Returns:
            Tuple of (merged_strategies, stats)
        """
        merged = existing.copy()
        stats = {"strategies_added": 0, "strategies_merged": 0}

        for new_strategy in new:
            # Find similar existing strategy
            similar_idx = self._find_similar_strategy(
                new_strategy,
                merged,
                similarity_threshold,
            )

            if similar_idx is not None:
                # Merge with existing strategy
                merged[similar_idx] = self._merge_similar_strategies(
                    merged[similar_idx],
                    new_strategy,
                )
                stats["strategies_merged"] += 1
            else:
                # Add as new strategy
                merged.append(new_strategy)
                stats["strategies_added"] += 1

        return merged, stats

    def _find_similar_strategy(
        self,
        strategy: dict[str, Any],
        strategy_list: list[dict[str, Any]],
        threshold: float,
    ) -> int | None:
        """Find similar strategy in list.

        Args:
            strategy: Strategy to find similarity for
            strategy_list: List of strategies to search
            threshold: Similarity threshold

        Returns:
            Index of similar strategy or None if not found

        Note:
            This is a simple keyword-based similarity. For production,
            use semantic embeddings and cosine similarity.
        """
        strategy_desc = strategy.get("description", "").lower()
        strategy_type = strategy.get("type", "")

        for idx, existing in enumerate(strategy_list):
            # Must be same type
            if existing.get("type") != strategy_type:
                continue

            existing_desc = existing.get("description", "").lower()

            # Simple keyword overlap similarity
            strategy_words = set(strategy_desc.split())
            existing_words = set(existing_desc.split())

            if not strategy_words or not existing_words:
                continue

            overlap = len(strategy_words & existing_words)
            union = len(strategy_words | existing_words)
            similarity = overlap / union if union > 0 else 0.0

            if similarity >= threshold:
                return idx

        return None

    def _merge_similar_strategies(
        self,
        existing: dict[str, Any],
        new: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge two similar strategies.

        Args:
            existing: Existing strategy
            new: New strategy to merge in

        Returns:
            Merged strategy
        """
        merged = existing.copy()

        # Combine evidence
        existing_evidence = existing.get("evidence", "")
        new_evidence = new.get("evidence", "")
        if new_evidence and new_evidence != existing_evidence:
            merged["evidence"] = f"{existing_evidence}; {new_evidence}"

        # Update confidence (weighted average favoring higher confidence)
        existing_conf = existing.get("confidence", 0.5)
        new_conf = new.get("confidence", 0.5)
        merged["confidence"] = max(existing_conf, new_conf)

        # Track sources
        existing_sources = merged.get("source_trajectories", [])
        if not isinstance(existing_sources, list):
            existing_sources = (
                [merged.get("source_trajectory")] if merged.get("source_trajectory") else []
            )

        new_source = new.get("source_trajectory")
        if new_source and new_source not in existing_sources:
            existing_sources.append(new_source)

        merged["source_trajectories"] = existing_sources
        merged["last_reinforced"] = datetime.now(UTC).isoformat()

        return merged
