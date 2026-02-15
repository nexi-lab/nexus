"""Memory evolution detector for automatic relationship detection (#1190).

Detects semantic relationships between new and existing memories:
- UPDATES: New info supersedes old (e.g., "Bob joined Google" supersedes "Bob works at Acme")
- EXTENDS: New info adds detail (e.g., "Bob specializes in ML" extends "Bob is an engineer")
- DERIVES: Logical consequence (e.g., "We should cut costs" derives from "Q3 revenue below target")

Uses a hybrid heuristic+LLM approach mirroring stability_classifier.py:
- Heuristic classifier handles ~80% of cases via regex + entity overlap + embedding similarity
- LLM escalation for ambiguous cases (confidence < 0.7)

Usage:
    detector = MemoryEvolutionDetector()
    result = detector.detect(
        session=session, zone_id="default",
        new_text="Alice now works at Google",
        new_entities=[{"text": "Alice", "label": "PERSON"}],
        person_refs="Alice", entity_types="PERSON",
        embedding_vec=[0.1, 0.2, ...],
    )
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from nexus.storage.models.memory import MemoryModel

logger = logging.getLogger(__name__)

# Maximum text length for classification (performance optimization)
MAX_CLASSIFICATION_TEXT_LENGTH = 500

# Default thresholds
DEFAULT_CONFIDENCE_THRESHOLD = 0.7
DEFAULT_SIMILARITY_THRESHOLD = 0.3
DEFAULT_MAX_CANDIDATES = 10
DEFAULT_TIMEOUT_MS = 200.0


@dataclass(frozen=True)
class EvolutionResult:
    """Result of a single evolution relationship detection.

    Attributes:
        relationship_type: "UPDATES", "EXTENDS", "DERIVES", or None.
        target_memory_id: ID of the related existing memory.
        confidence: Confidence score from 0.0 to 1.0.
        method: Detection method ("heuristic", "llm", "none", "error").
        signals: List of signals that contributed to the detection.
        elapsed_ms: Time taken for this detection in milliseconds.
    """

    relationship_type: str | None
    target_memory_id: str | None
    confidence: float
    method: str
    signals: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class EvolutionDetectionResult:
    """Aggregate result of evolution detection across all candidates.

    Attributes:
        relationships: Tuple of detected evolution relationships.
        candidates_evaluated: Number of candidates evaluated.
        elapsed_ms: Total time taken in milliseconds.
    """

    relationships: tuple[EvolutionResult, ...] = ()
    candidates_evaluated: int = 0
    elapsed_ms: float = 0.0


# Pre-compiled regex patterns for heuristic classification
UPDATES_MARKERS = re.compile(
    r"\b(actually|correction|no longer|changed to|is now|instead|"
    r"not anymore|switched to|moved to|replaced|updated|"
    r"previously|was wrong|turns out|in fact|corrected)\b",
    re.IGNORECASE,
)

EXTENDS_MARKERS = re.compile(
    r"\b(also|additionally|furthermore|moreover|more specifically|"
    r"in addition|on top of|besides|plus|as well as|"
    r"another thing|not only|along with|together with)\b",
    re.IGNORECASE,
)

DERIVES_MARKERS = re.compile(
    r"\b(therefore|thus|consequently|because of|based on|implies|"
    r"as a result|it follows|hence|so we should|"
    r"this means|given that|due to|leads to|suggests that)\b",
    re.IGNORECASE,
)


def _compute_cosine_similarity_pure(vec_a: list[float], vec_b: list[float]) -> float:
    """Compute cosine similarity between two vectors (pure Python fallback).

    Args:
        vec_a: First vector.
        vec_b: Second vector.

    Returns:
        Cosine similarity in range [-1, 1], or 0 if either vector is zero.
    """
    a = np.array(vec_a, dtype=np.float64)
    b = np.array(vec_b, dtype=np.float64)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def _compute_entity_overlap(
    new_entities: list[dict[str, Any]],
    existing_person_refs: str | None,
    existing_entity_types: str | None,
) -> float:
    """Compute entity overlap ratio between new and existing memory.

    Args:
        new_entities: Parsed entity list from the new memory.
        existing_person_refs: Comma-separated person names from existing memory.
        existing_entity_types: Comma-separated entity types from existing memory.

    Returns:
        Overlap ratio between 0.0 and 1.0.
    """
    if not new_entities:
        return 0.0

    existing_persons = set()
    if existing_person_refs:
        existing_persons = {p.strip().lower() for p in existing_person_refs.split(",") if p.strip()}

    existing_types = set()
    if existing_entity_types:
        existing_types = {t.strip() for t in existing_entity_types.split(",") if t.strip()}

    overlap_count: float = 0
    total_count = len(new_entities)

    for entity in new_entities:
        name = (entity.get("text") or entity.get("name", "")).lower()
        etype = entity.get("label") or entity.get("type", "")

        if name and name in existing_persons:
            overlap_count += 1
        elif etype and etype in existing_types:
            overlap_count += 0.5

    return min(1.0, overlap_count / max(1, total_count))


class MemoryEvolutionDetector:
    """Hybrid heuristic+LLM detector for memory evolution relationships.

    Mirrors the structure of TemporalStabilityClassifier:
    - Heuristic classification for clear cases
    - LLM escalation for ambiguous cases (confidence < threshold)

    Args:
        llm_provider: Optional LLM provider for ambiguous cases.
        confidence_threshold: Minimum heuristic confidence to skip LLM (default 0.7).
        similarity_threshold: Minimum embedding similarity to consider a candidate (default 0.3).
        max_candidates: Maximum candidates to evaluate (default 10).
    """

    def __init__(
        self,
        llm_provider: Any = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
    ):
        self.llm_provider = llm_provider
        self.confidence_threshold = confidence_threshold
        self.similarity_threshold = similarity_threshold
        self.max_candidates = max_candidates

    def detect(
        self,
        session: Session,
        zone_id: str | None,
        new_text: str,
        new_entities: list[dict[str, Any]] | None = None,
        person_refs: str | None = None,
        entity_types: str | None = None,
        embedding_vec: list[float] | None = None,
        exclude_memory_id: str | None = None,
        timeout_ms: float = DEFAULT_TIMEOUT_MS,
    ) -> EvolutionDetectionResult:
        """Detect evolution relationships between new text and existing memories.

        Args:
            session: Database session.
            zone_id: Zone ID for isolation.
            new_text: Text content of the new memory.
            new_entities: Parsed entities from the new memory.
            person_refs: Comma-separated person names from the new memory.
            entity_types: Comma-separated entity types from the new memory.
            embedding_vec: Embedding vector for similarity comparison.
            exclude_memory_id: Memory ID to exclude (e.g., self).
            timeout_ms: Soft timeout in milliseconds (default 200ms).

        Returns:
            EvolutionDetectionResult with detected relationships.
        """
        start_time = time.monotonic()

        # Truncate text for classification
        truncated_text = new_text[:MAX_CLASSIFICATION_TEXT_LENGTH]

        try:
            # Step 1: Find candidate memories
            candidates = self._find_candidates(
                session=session,
                zone_id=zone_id,
                person_refs=person_refs,
                entity_types=entity_types,
                embedding_vec=embedding_vec,
                exclude_memory_id=exclude_memory_id,
            )

            if not candidates:
                elapsed = (time.monotonic() - start_time) * 1000
                logger.debug("Evolution detection: no candidates found (%.1fms)", elapsed)
                return EvolutionDetectionResult(elapsed_ms=elapsed)

            # Step 2: Classify each candidate
            results: list[EvolutionResult] = []
            for candidate, similarity in candidates:
                # Check soft timeout
                elapsed = (time.monotonic() - start_time) * 1000
                if elapsed > timeout_ms:
                    logger.debug(
                        "Evolution detection: soft timeout after %d candidates (%.1fms)",
                        len(results),
                        elapsed,
                    )
                    break

                result = self._classify_candidate(
                    new_text=truncated_text,
                    candidate=candidate,
                    similarity=similarity,
                    new_entities=new_entities,
                )

                if result.relationship_type is not None:
                    results.append(result)

            elapsed = (time.monotonic() - start_time) * 1000
            logger.info(
                "Evolution detected: %d relationships in %.1fms (%d candidates)",
                len(results),
                elapsed,
                len(candidates),
            )

            return EvolutionDetectionResult(
                relationships=tuple(results),
                candidates_evaluated=len(candidates),
                elapsed_ms=elapsed,
            )

        except Exception:
            elapsed = (time.monotonic() - start_time) * 1000
            logger.warning(
                "Evolution detection failed (%.1fms), continuing without it",
                elapsed,
                exc_info=True,
            )
            return EvolutionDetectionResult(elapsed_ms=elapsed)

    def _find_candidates(
        self,
        session: Session,
        zone_id: str | None,
        person_refs: str | None = None,
        entity_types: str | None = None,
        embedding_vec: list[float] | None = None,
        exclude_memory_id: str | None = None,
    ) -> list[tuple[MemoryModel, float]]:
        """Find candidate memories for evolution detection (two-phase query).

        Phase 1: SQL query filtering by zone, state, entity overlap.
        Phase 2: Cosine similarity rerank using embeddings.

        Args:
            session: Database session.
            zone_id: Zone ID for isolation.
            person_refs: Comma-separated person names to match.
            entity_types: Comma-separated entity types to match.
            embedding_vec: Embedding vector for reranking.
            exclude_memory_id: Memory ID to exclude.

        Returns:
            List of (MemoryModel, similarity_score) tuples, sorted by similarity desc.
        """
        # Phase 1: SQL query with entity overlap filters
        stmt = select(MemoryModel).where(
            MemoryModel.state == "active",
            MemoryModel.invalid_at.is_(None),
            MemoryModel.superseded_by_id.is_(None),
        )

        if zone_id:
            stmt = stmt.where(MemoryModel.zone_id == zone_id)

        if exclude_memory_id:
            stmt = stmt.where(MemoryModel.memory_id != exclude_memory_id)

        # Entity overlap filter: match on person refs or entity types
        entity_filters = []
        if person_refs:
            for name in person_refs.split(","):
                name = name.strip()
                if name:
                    entity_filters.append(MemoryModel.person_refs.like(f"%{name}%"))
        if entity_types:
            for etype in entity_types.split(","):
                etype = etype.strip()
                if etype:
                    entity_filters.append(MemoryModel.entity_types.like(f"%{etype}%"))

        if entity_filters:
            from sqlalchemy import or_

            stmt = stmt.where(or_(*entity_filters))
        else:
            # No entity filters — skip candidate search to avoid scanning entire table
            return []

        # Limit to 100 candidates before reranking
        stmt = stmt.limit(100)

        raw_candidates = list(session.execute(stmt).scalars().all())

        if not raw_candidates:
            return []

        # Phase 2: Cosine similarity rerank
        if embedding_vec:
            scored: list[tuple[MemoryModel, float]] = []
            for candidate in raw_candidates:
                if candidate.embedding:
                    try:
                        candidate_vec = json.loads(candidate.embedding)
                        sim = _compute_cosine_similarity_pure(embedding_vec, candidate_vec)
                        if sim >= self.similarity_threshold:
                            scored.append((candidate, sim))
                    except (json.JSONDecodeError, TypeError):
                        # Skip candidates with invalid embeddings
                        scored.append((candidate, 0.0))
                else:
                    scored.append((candidate, 0.0))

            # Sort by similarity descending
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[: self.max_candidates]
        else:
            # No embedding available — return all candidates with score 0
            return [(c, 0.0) for c in raw_candidates[: self.max_candidates]]

    def _classify_candidate(
        self,
        new_text: str,
        candidate: MemoryModel,
        similarity: float,
        new_entities: list[dict[str, Any]] | None = None,
    ) -> EvolutionResult:
        """Classify the relationship between new text and a candidate memory.

        Uses heuristic classification first, then escalates to LLM if ambiguous.

        Args:
            new_text: Truncated text of the new memory.
            candidate: Candidate existing memory.
            similarity: Embedding similarity score.
            new_entities: Parsed entities from the new memory.

        Returns:
            EvolutionResult with classification.
        """
        start_time = time.monotonic()

        # Step 1: Heuristic classification
        heuristic_result = self._classify_heuristic(
            new_text=new_text,
            candidate=candidate,
            similarity=similarity,
            new_entities=new_entities,
        )

        # Step 2: If confidence is high enough, return heuristic result
        if heuristic_result.confidence >= self.confidence_threshold:
            return heuristic_result

        # Step 3: LLM escalation for ambiguous cases
        if self.llm_provider is not None and heuristic_result.relationship_type is not None:
            try:
                # Get candidate text from content
                candidate_text = self._get_candidate_text(candidate)
                llm_result = self._classify_llm(
                    new_text=new_text,
                    existing_text=candidate_text,
                    candidate_memory_id=candidate.memory_id,
                )
                return llm_result
            except Exception:
                logger.warning(
                    "LLM evolution classification failed, falling back to heuristic",
                    exc_info=True,
                )
                elapsed = (time.monotonic() - start_time) * 1000
                return EvolutionResult(
                    relationship_type=heuristic_result.relationship_type,
                    target_memory_id=heuristic_result.target_memory_id,
                    confidence=heuristic_result.confidence,
                    method="error",
                    signals=[*heuristic_result.signals, "llm_fallback"],
                    elapsed_ms=elapsed,
                )

        return heuristic_result

    def _classify_heuristic(
        self,
        new_text: str,
        candidate: MemoryModel,
        similarity: float,
        new_entities: list[dict[str, Any]] | None = None,
    ) -> EvolutionResult:
        """Classify using regex patterns, entity overlap, and embedding similarity.

        Scoring: regex matches (1.0 weight) + entity overlap ratio + embedding similarity.
        Confidence = winner_score / total_score.

        Args:
            new_text: New memory text.
            candidate: Existing candidate memory.
            similarity: Pre-computed embedding similarity.
            new_entities: Parsed entities from new memory.

        Returns:
            EvolutionResult with heuristic classification.
        """
        start_time = time.monotonic()
        signals: list[str] = []

        # Score accumulators
        updates_score = 0.0
        extends_score = 0.0
        derives_score = 0.0

        # 1. Check regex pattern matches on new text
        updates_matches = UPDATES_MARKERS.findall(new_text)
        for match in updates_matches:
            signals.append(f"updates_marker:{match.lower()}")
            updates_score += 1.0

        extends_matches = EXTENDS_MARKERS.findall(new_text)
        for match in extends_matches:
            signals.append(f"extends_marker:{match.lower()}")
            extends_score += 1.0

        derives_matches = DERIVES_MARKERS.findall(new_text)
        for match in derives_matches:
            signals.append(f"derives_marker:{match.lower()}")
            derives_score += 1.0

        # 2. Entity overlap (shared entities suggest UPDATES or EXTENDS)
        entity_overlap = _compute_entity_overlap(
            new_entities or [],
            candidate.person_refs,
            candidate.entity_types,
        )
        if entity_overlap > 0:
            signals.append(f"entity_overlap:{entity_overlap:.2f}")
            # High entity overlap with update markers → UPDATES
            # High entity overlap without update markers → EXTENDS
            updates_score += entity_overlap * 0.5
            extends_score += entity_overlap * 0.3

        # 3. Embedding similarity boost
        if similarity > 0:
            signals.append(f"embedding_similarity:{similarity:.2f}")
            # High similarity suggests UPDATES (same topic, newer info)
            if similarity > 0.8:
                updates_score += 0.3
            elif similarity > 0.5:
                extends_score += 0.2
                updates_score += 0.1

        # 4. Determine winner
        total_score = updates_score + extends_score + derives_score

        elapsed = (time.monotonic() - start_time) * 1000

        if total_score == 0:
            return EvolutionResult(
                relationship_type=None,
                target_memory_id=candidate.memory_id,
                confidence=0.0,
                method="heuristic",
                signals=["no_signals_detected"],
                elapsed_ms=elapsed,
            )

        scores = {
            "UPDATES": updates_score,
            "EXTENDS": extends_score,
            "DERIVES": derives_score,
        }
        winner = max(scores, key=scores.get)  # type: ignore[arg-type]
        winner_score = scores[winner]

        # Confidence = winner's proportion of total score
        confidence = min(0.95, winner_score / total_score)

        # Boost confidence if only one category has signals
        nonzero_categories = sum(1 for s in scores.values() if s > 0)
        if nonzero_categories == 1:
            confidence = min(0.95, confidence + 0.2)

        # Minimum confidence threshold to report any relationship
        if confidence < 0.3:
            return EvolutionResult(
                relationship_type=None,
                target_memory_id=candidate.memory_id,
                confidence=confidence,
                method="heuristic",
                signals=signals,
                elapsed_ms=elapsed,
            )

        return EvolutionResult(
            relationship_type=winner,
            target_memory_id=candidate.memory_id,
            confidence=round(confidence, 2),
            method="heuristic",
            signals=signals,
            elapsed_ms=elapsed,
        )

    def _classify_llm(
        self,
        new_text: str,
        existing_text: str,
        candidate_memory_id: str,
    ) -> EvolutionResult:
        """Classify using LLM for ambiguous cases.

        Args:
            new_text: New memory text.
            existing_text: Existing candidate memory text.
            candidate_memory_id: ID of the candidate memory.

        Returns:
            EvolutionResult with LLM classification.
        """
        start_time = time.monotonic()

        prompt = (
            "Classify the relationship between a new memory and an existing memory.\n\n"
            f'New memory: "{new_text}"\n'
            f'Existing memory: "{existing_text}"\n\n'
            "Categories:\n"
            "- UPDATES: New memory supersedes/corrects the existing one "
            "(same topic, newer or corrected info)\n"
            "- EXTENDS: New memory adds detail to the existing one "
            "(additional info about same entity/topic)\n"
            "- DERIVES: New memory is a logical consequence of the existing one "
            "(conclusion, implication, action item)\n"
            "- NONE: No meaningful relationship\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"relationship": "UPDATES|EXTENDS|DERIVES|NONE", '
            '"confidence": 0.0-1.0, '
            '"reasoning": "brief explanation"}'
        )

        response = self.llm_provider.generate(prompt)
        response_text = response if isinstance(response, str) else str(response)

        # Parse JSON response
        json_match = re.search(r"\{[^}]+\}", response_text)
        if not json_match:
            raise ValueError(f"No JSON found in LLM response: {response_text[:200]}")

        parsed = json.loads(json_match.group())

        relationship = parsed.get("relationship", "NONE")
        valid_types = ("UPDATES", "EXTENDS", "DERIVES", "NONE")
        if relationship not in valid_types:
            relationship = "NONE"

        confidence = float(parsed.get("confidence", 0.7))
        confidence = max(0.0, min(1.0, confidence))

        elapsed = (time.monotonic() - start_time) * 1000

        return EvolutionResult(
            relationship_type=relationship if relationship != "NONE" else None,
            target_memory_id=candidate_memory_id,
            confidence=round(confidence, 2),
            method="llm",
            signals=[f"llm_reasoning:{parsed.get('reasoning', 'n/a')[:100]}"],
            elapsed_ms=elapsed,
        )

    @staticmethod
    def _get_candidate_text(candidate: MemoryModel) -> str:
        """Extract text representation from a candidate memory for LLM classification.

        Uses entities_json as a proxy since we don't have the raw content here.

        Args:
            candidate: Candidate memory model.

        Returns:
            Text representation of the candidate.
        """
        parts = []
        if candidate.person_refs:
            parts.append(f"People: {candidate.person_refs}")
        if candidate.entity_types:
            parts.append(f"Types: {candidate.entity_types}")
        if candidate.entities_json:
            try:
                entities = json.loads(candidate.entities_json)
                entity_texts: list[str] = [
                    e.get("text") or e.get("name", "") for e in entities if isinstance(e, dict)
                ]
                if entity_texts:
                    parts.append(f"Entities: {', '.join(entity_texts)}")
            except (json.JSONDecodeError, TypeError):
                pass
        return "; ".join(parts) if parts else "<no entity info>"


def apply_evolution_results(
    session: Session,
    new_memory_id: str,
    results: EvolutionDetectionResult,
) -> None:
    """Apply detected evolution relationships to the database.

    For UPDATES: set supersedes_id on new memory, superseded_by_id + invalid_at on target.
    For EXTENDS: build extends_ids JSON array for new memory, append to target's extended_by_ids.
    For DERIVES: build derived_from_ids JSON array for new memory.

    Args:
        session: Database session.
        new_memory_id: ID of the newly created memory.
        results: Evolution detection results.
    """
    if not results.relationships:
        return

    extends_targets: list[str] = []
    derives_targets: list[str] = []

    for rel in results.relationships:
        if rel.relationship_type is None or rel.target_memory_id is None:
            continue

        if rel.relationship_type == "UPDATES":
            # Reuse existing supersedes pattern
            _apply_updates_relationship(
                session=session,
                new_memory_id=new_memory_id,
                target_memory_id=rel.target_memory_id,
            )

        elif rel.relationship_type == "EXTENDS":
            extends_targets.append(rel.target_memory_id)
            # Add back-link to target's extended_by_ids
            _append_to_json_array(
                session=session,
                memory_id=rel.target_memory_id,
                field_name="extended_by_ids",
                value=new_memory_id,
            )

        elif rel.relationship_type == "DERIVES":
            derives_targets.append(rel.target_memory_id)

    # Set forward links on the new memory
    updates: dict[str, Any] = {}
    if extends_targets:
        updates["extends_ids"] = json.dumps(extends_targets)
    if derives_targets:
        updates["derived_from_ids"] = json.dumps(derives_targets)

    if updates:
        new_memory = session.get(MemoryModel, new_memory_id)
        if new_memory:
            for key, value in updates.items():
                setattr(new_memory, key, value)
            session.commit()


def _apply_updates_relationship(
    session: Session,
    new_memory_id: str,
    target_memory_id: str,
) -> None:
    """Apply an UPDATES relationship (supersedes pattern).

    Sets supersedes_id on new memory, superseded_by_id + invalid_at on target.

    Args:
        session: Database session.
        new_memory_id: ID of the new memory.
        target_memory_id: ID of the target (superseded) memory.
    """
    from datetime import UTC, datetime

    new_memory = session.get(MemoryModel, new_memory_id)
    target_memory = session.get(MemoryModel, target_memory_id)

    if not new_memory or not target_memory:
        return

    # Only update if target isn't already superseded
    if target_memory.superseded_by_id is not None:
        return

    new_memory.supersedes_id = target_memory_id
    target_memory.superseded_by_id = new_memory_id
    target_memory.invalid_at = datetime.now(UTC)
    session.commit()


def _append_to_json_array(
    session: Session,
    memory_id: str,
    field_name: str,
    value: str,
) -> None:
    """Append a value to a JSON array field on a memory.

    Args:
        session: Database session.
        memory_id: Memory ID to update.
        field_name: Name of the JSON array field.
        value: Value to append.
    """
    memory = session.get(MemoryModel, memory_id)
    if not memory:
        return

    current_value = getattr(memory, field_name, None)
    if current_value:
        try:
            arr = json.loads(current_value)
            if not isinstance(arr, list):
                arr = [current_value]
        except (json.JSONDecodeError, TypeError):
            arr = []
    else:
        arr = []

    if value not in arr:
        arr.append(value)
        setattr(memory, field_name, json.dumps(arr))
        session.commit()
