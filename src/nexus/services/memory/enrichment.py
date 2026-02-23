"""Memory enrichment pipeline (#1498).

Extracts the 8-step enrichment pipeline from Memory.store() into a
composable, independently testable class.

Each enrichment step follows the pattern:
    - Check if enabled (flag + text content available)
    - Lazy-import the required module
    - Run enrichment, populate result dataclass
    - On failure: log warning, continue (non-fatal)

Usage:
    pipeline = EnrichmentPipeline(llm_provider=llm)
    result = pipeline.enrich(text_content, flags)
    # result.embedding_json, result.entities_json, etc.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentFlags:
    """Configuration flags controlling which enrichment steps to run."""

    generate_embedding: bool = True
    extract_entities: bool = True
    extract_temporal: bool = True
    extract_relationships: bool = False
    classify_stability: bool = True
    detect_evolution: bool = False
    resolve_coreferences: bool = False
    resolve_temporal: bool = False
    store_to_graph: bool = False

    # Parameters for specific steps
    embedding_provider: Any = None
    coreference_context: str | None = None
    temporal_reference_time: Any = None
    relationship_types: list[str] | None = None


@dataclass
class EnrichmentResult:
    """Output of the enrichment pipeline — all metadata produced by enrichment steps."""

    # Embedding (#406)
    embedding_json: str | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None

    # Entities (#1025)
    entities_json: str | None = None
    entity_types: str | None = None
    person_refs: str | None = None
    parsed_entities: list[dict[str, Any]] | None = None

    # Temporal metadata (#1028)
    temporal_refs_json: str | None = None
    earliest_date: datetime | None = None
    latest_date: datetime | None = None

    # Relationships (#1038)
    relationships_json: str | None = None
    relationship_count: int | None = None

    # Stability classification (#1191)
    temporal_stability: str | None = None
    stability_confidence: float | None = None
    estimated_ttl_days: int | None = None


class EnrichmentPipeline:
    """Composable enrichment pipeline for memory content.

    Each step is independently testable and failure-tolerant.
    Steps share intermediate results (e.g., parsed entities are reused
    by relationship extraction and stability classification).
    """

    def __init__(self, llm_provider: Any = None) -> None:
        self._llm_provider = llm_provider

    def resolve_content(
        self,
        content: str,
        flags: EnrichmentFlags,
    ) -> str:
        """Apply write-time content transformations (coreference + temporal resolution).

        These transformations modify the content text itself before storage,
        making memories self-contained and context-independent.

        Args:
            content: Original text content.
            flags: Enrichment configuration.

        Returns:
            Transformed content string.
        """
        if flags.resolve_coreferences:
            content = self._resolve_coreferences(content, flags.coreference_context)
        if flags.resolve_temporal:
            content = self._resolve_temporal(content, flags.temporal_reference_time)
        return content

    def enrich(
        self,
        text_content: str | None,
        flags: EnrichmentFlags,
    ) -> EnrichmentResult:
        """Run all enrichment steps on text content.

        Args:
            text_content: Extracted text from content (may be None for binary).
            flags: Configuration controlling which steps to run.

        Returns:
            EnrichmentResult with all populated metadata fields.
        """
        result = EnrichmentResult()

        if not text_content:
            return result

        # Step 1: Generate embedding (#406)
        if flags.generate_embedding:
            self._enrich_embedding(text_content, flags.embedding_provider, result)

        # Step 2: Extract entities (#1025)
        if flags.extract_entities:
            self._enrich_entities(text_content, result)

        # Step 3: Extract temporal metadata (#1028)
        if flags.extract_temporal:
            self._enrich_temporal(text_content, flags.temporal_reference_time, result)

        # Step 4: Extract relationships (#1038)
        if flags.extract_relationships:
            self._enrich_relationships(
                text_content, result.parsed_entities, flags.relationship_types, result
            )

        # Step 5: Classify temporal stability (#1191)
        if flags.classify_stability:
            self._enrich_stability(text_content, result)

        return result

    def _resolve_coreferences(self, text: str, context: str | None) -> str:
        """Apply coreference resolution (#1027)."""
        try:
            from nexus.services.memory.coref_resolver import resolve_coreferences as resolve_coref

            return resolve_coref(
                text=text,
                context=context,
                llm_provider=self._llm_provider,
            )
        except Exception:
            logger.warning("Coreference resolution failed, using original text", exc_info=True)
            return text

    def _resolve_temporal(self, text: str, reference_time: Any) -> str:
        """Apply temporal expression resolution (#1027)."""
        try:
            from nexus.core.temporal_resolver import resolve_temporal as resolve_temp

            return resolve_temp(
                text=text,
                reference_time=reference_time,
                llm_provider=self._llm_provider,
            )
        except Exception:
            logger.warning("Temporal resolution failed, using original text", exc_info=True)
            return text

    def _enrich_embedding(
        self, text: str, embedding_provider: Any, result: EnrichmentResult
    ) -> None:
        """Generate embedding vector (#406)."""
        provider = embedding_provider
        if provider is None:
            try:
                from nexus.search.embeddings import create_embedding_provider

                try:
                    provider = create_embedding_provider(provider="openrouter")
                except Exception as e:
                    logger.debug("Failed to create embedding provider: %s", e)
            except ImportError:
                return

        if not provider:
            return

        try:
            from nexus.core.sync_bridge import run_sync

            embedding_vec = run_sync(provider.embed_text(text))
            result.embedding_json = json.dumps(embedding_vec)
            result.embedding_model = getattr(provider, "model", "unknown")
            result.embedding_dim = len(embedding_vec)
        except Exception:
            logger.warning("Embedding generation failed", exc_info=True)

    def _enrich_entities(self, text: str, result: EnrichmentResult) -> None:
        """Extract named entities (#1025)."""
        try:
            from nexus.bricks.rebac.entity_extractor import EntityExtractor

            extractor = EntityExtractor(use_spacy=False)
            entities = extractor.extract(text)

            if entities:
                result.entities_json = json.dumps([e.to_dict() for e in entities])
                result.entity_types = extractor.get_entity_types_string(text)
                result.person_refs = extractor.get_person_refs_string(text)
                result.parsed_entities = json.loads(result.entities_json)
        except Exception:
            logger.warning("Entity extraction failed", exc_info=True)

    def _enrich_temporal(self, text: str, reference_time: Any, result: EnrichmentResult) -> None:
        """Extract temporal metadata (#1028)."""
        try:
            from nexus.core.temporal_resolver import extract_temporal_metadata

            temporal_meta = extract_temporal_metadata(text, reference_time=reference_time)

            if temporal_meta["temporal_refs"]:
                result.temporal_refs_json = json.dumps(temporal_meta["temporal_refs"])
                result.earliest_date = temporal_meta["earliest_date"]
                result.latest_date = temporal_meta["latest_date"]
        except Exception:
            logger.warning("Temporal extraction failed", exc_info=True)

    def _enrich_relationships(
        self,
        text: str,
        parsed_entities: list[dict[str, Any]] | None,
        relationship_types: list[str] | None,
        result: EnrichmentResult,
    ) -> None:
        """Extract relationships (#1038)."""
        try:
            from nexus.services.memory.relationship_extractor import LLMRelationshipExtractor

            rel_extractor = LLMRelationshipExtractor(
                llm_provider=self._llm_provider,
                confidence_threshold=0.5,
            )
            rel_result = rel_extractor.extract(
                text,
                entities=parsed_entities,
                relationship_types=relationship_types,
            )

            if rel_result.relationships:
                result.relationships_json = json.dumps(rel_result.to_dicts())
                result.relationship_count = len(rel_result.relationships)
        except Exception:
            logger.warning("Relationship extraction failed", exc_info=True)

    def _enrich_stability(self, text: str, result: EnrichmentResult) -> None:
        """Classify temporal stability (#1191)."""
        try:
            from nexus.services.memory.stability_classifier import TemporalStabilityClassifier

            classifier = TemporalStabilityClassifier(llm_provider=self._llm_provider)

            temporal_refs_for_classify = None
            if result.temporal_refs_json:
                temporal_refs_for_classify = json.loads(result.temporal_refs_json)

            classification = classifier.classify(
                text=text,
                entities=result.parsed_entities,
                temporal_refs=temporal_refs_for_classify,
            )

            result.temporal_stability = classification.temporal_stability
            result.stability_confidence = classification.confidence
            result.estimated_ttl_days = classification.estimated_ttl_days
        except Exception:
            logger.warning("Stability classification failed, continuing without it", exc_info=True)
