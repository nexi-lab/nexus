"""Temporal stability classifier for memory auto-classification (#1191).

Classifies memories as static, semi-dynamic, or dynamic using a hybrid
heuristic+LLM approach. The heuristic classifier handles ~70-80% of cases;
ambiguous cases (confidence < threshold) are escalated to the LLM.

Usage:
    classifier = TemporalStabilityClassifier()
    result = classifier.classify("Paris is the capital of France")
    # StabilityClassification(temporal_stability="static", confidence=0.95, ...)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Maximum text length for classification (performance optimization)
MAX_CLASSIFICATION_TEXT_LENGTH = 500

# Valid stability values
VALID_STABILITIES = ("static", "semi_dynamic", "dynamic")


@dataclass(frozen=True)
class StabilityClassification:
    """Result of temporal stability classification.

    Attributes:
        temporal_stability: One of "static", "semi_dynamic", "dynamic".
        confidence: Confidence score from 0.0 to 1.0.
        estimated_ttl_days: Estimated time-to-live in days. None = infinite/static.
        method: Classification method used ("heuristic", "llm", "hybrid").
        signals: List of signals that contributed to the classification.
    """

    temporal_stability: str
    confidence: float
    estimated_ttl_days: int | None
    method: str
    signals: list[str] = field(default_factory=list)


# Pre-compiled regex patterns for heuristic classification
STATIC_MARKERS = re.compile(
    r"\b(always|never|by definition|was born|founded in|is the capital|"
    r"discovered|invented|pi equals|boiling point|atomic number|"
    r"mathematical|theorem|law of|constant|universal|permanent|"
    r"immutable|eternal|historical fact|died on|died in)\b",
    re.IGNORECASE,
)

DYNAMIC_MARKERS = re.compile(
    r"\b(currently|right now|today|at the moment|this week|lately|"
    r"as of now|presently|at present|just now|recently started|"
    r"is now|has just|since yesterday|this month|this year|"
    r"breaking news|latest|ongoing|in progress)\b",
    re.IGNORECASE,
)

SEMI_DYNAMIC_MARKERS = re.compile(
    r"\b(works at|lives in|is studying|prefers|usually|typically|"
    r"employed by|resides in|enrolled at|married to|dating|"
    r"manages|leads|heads|is a member of|belongs to|"
    r"drives a|owns a|rents|subscribes to)\b",
    re.IGNORECASE,
)

# Entity-attribute ontology: maps (entity_type, attribute_pattern) → stability
ENTITY_STABILITY_MAP: dict[str, str] = {
    "PERSON.birth": "static",
    "PERSON.death": "static",
    "PERSON.nationality": "semi_dynamic",
    "PERSON.job_title": "dynamic",
    "PERSON.employer": "semi_dynamic",
    "PERSON.residence": "semi_dynamic",
    "PERSON.relationship": "semi_dynamic",
    "ORG.founded": "static",
    "ORG.headquarters": "semi_dynamic",
    "ORG.ceo": "dynamic",
    "ORG.revenue": "dynamic",
    "ORG.employees": "dynamic",
    "LOCATION.capital": "static",
    "LOCATION.population": "dynamic",
    "LOCATION.country": "static",
    "DATE": "static",
    "NUMBER.constant": "static",
    "NUMBER.measurement": "dynamic",
}

# Default TTL estimates (days) by stability
DEFAULT_TTL: dict[str, int | None] = {
    "static": None,  # Infinite
    "semi_dynamic": 365,  # ~1 year
    "dynamic": 30,  # ~1 month
}


class TemporalStabilityClassifier:
    """Hybrid heuristic+LLM classifier for memory temporal stability.

    Args:
        llm_provider: Optional LLM provider for ambiguous cases.
        confidence_threshold: Minimum heuristic confidence to skip LLM (default 0.6).
    """

    def __init__(
        self,
        llm_provider: Any = None,
        confidence_threshold: float = 0.6,
    ):
        self.llm_provider = llm_provider
        self.confidence_threshold = confidence_threshold

    def classify(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        temporal_refs: list[dict[str, Any]] | None = None,
    ) -> StabilityClassification:
        """Classify text for temporal stability.

        Args:
            text: Memory text content.
            entities: Extracted entities (optional, reused from enrichment pipeline).
            temporal_refs: Extracted temporal references (optional).

        Returns:
            StabilityClassification with stability, confidence, and metadata.
        """
        # Truncate text for classification performance
        truncated = text[:MAX_CLASSIFICATION_TEXT_LENGTH]

        # Step 1: Run heuristic classifier
        heuristic_result = self._classify_heuristic(truncated, entities, temporal_refs)

        # Step 2: If confidence is high enough, return heuristic result
        if heuristic_result.confidence >= self.confidence_threshold:
            return heuristic_result

        # Step 3: If LLM available, escalate for better classification
        if self.llm_provider is not None:
            try:
                llm_result = self._classify_llm(truncated)
                return llm_result
            except Exception:
                logger.warning(
                    "LLM classification failed, falling back to heuristic",
                    exc_info=True,
                )
                return heuristic_result

        # Step 4: No LLM available, return heuristic result (graceful degradation)
        return heuristic_result

    def _classify_heuristic(
        self,
        text: str,
        entities: list[dict[str, Any]] | None = None,
        temporal_refs: list[dict[str, Any]] | None = None,
    ) -> StabilityClassification:
        """Classify using regex patterns and entity ontology.

        Scores static/dynamic/semi-dynamic signals and returns the highest-scoring
        category with a confidence derived from signal strength.
        """
        signals: list[str] = []

        # Score accumulators
        static_score = 0.0
        dynamic_score = 0.0
        semi_dynamic_score = 0.0

        # 1. Check regex pattern matches
        static_matches = STATIC_MARKERS.findall(text)
        for match in static_matches:
            signals.append(f"static_marker:{match.lower()}")
            static_score += 1.0

        dynamic_matches = DYNAMIC_MARKERS.findall(text)
        for match in dynamic_matches:
            signals.append(f"dynamic_marker:{match.lower()}")
            dynamic_score += 1.0

        semi_dynamic_matches = SEMI_DYNAMIC_MARKERS.findall(text)
        for match in semi_dynamic_matches:
            signals.append(f"semi_dynamic_marker:{match.lower()}")
            semi_dynamic_score += 1.0

        # 2. Check entity-based signals
        if entities:
            for entity in entities:
                entity_type = entity.get("label", entity.get("type", ""))
                entity_text = entity.get("text", entity.get("name", "")).lower()

                # Check entity-attribute ontology
                for key, stability in ENTITY_STABILITY_MAP.items():
                    parts = key.split(".")
                    if len(parts) == 2:
                        etype, attr = parts
                        if entity_type == etype and attr in entity_text:
                            signals.append(f"entity:{key}")
                            if stability == "static":
                                static_score += 0.8
                            elif stability == "dynamic":
                                dynamic_score += 0.8
                            else:
                                semi_dynamic_score += 0.8
                    elif len(parts) == 1 and entity_type == parts[0]:
                        signals.append(f"entity_type:{entity_type}")
                        if stability == "static":
                            static_score += 0.5
                        elif stability == "dynamic":
                            dynamic_score += 0.5
                        else:
                            semi_dynamic_score += 0.5

        # 3. Temporal references as dynamic signal
        if temporal_refs:
            signals.append(f"temporal_refs_count:{len(temporal_refs)}")
            # Temporal references suggest time-sensitivity
            dynamic_score += 0.3 * len(temporal_refs)

        # 4. Calculate total and determine winner
        total_score = static_score + dynamic_score + semi_dynamic_score

        if total_score == 0:
            # No signals detected — default to semi_dynamic with low confidence
            return StabilityClassification(
                temporal_stability="semi_dynamic",
                confidence=0.3,
                estimated_ttl_days=DEFAULT_TTL["semi_dynamic"],
                method="heuristic",
                signals=["no_signals_detected"],
            )

        # Determine winning category
        scores = {
            "static": static_score,
            "semi_dynamic": semi_dynamic_score,
            "dynamic": dynamic_score,
        }
        winner = max(scores, key=scores.get)  # type: ignore[arg-type]
        winner_score = scores[winner]

        # Confidence = winner's proportion of total score, with a floor
        confidence = min(0.95, winner_score / total_score)

        # Boost confidence if only one category has signals
        nonzero_categories = sum(1 for s in scores.values() if s > 0)
        if nonzero_categories == 1:
            confidence = min(0.95, confidence + 0.2)

        return StabilityClassification(
            temporal_stability=winner,
            confidence=round(confidence, 2),
            estimated_ttl_days=DEFAULT_TTL[winner],
            method="heuristic",
            signals=signals,
        )

    def _classify_llm(self, text: str) -> StabilityClassification:
        """Classify using LLM for ambiguous cases.

        Uses a structured prompt to get a classification from the LLM provider.
        """
        prompt = (
            "Classify the temporal stability of this memory/fact.\n\n"
            f'Text: "{text}"\n\n'
            "Categories:\n"
            "- static: Facts that rarely or never change (e.g., historical dates, "
            "scientific constants, geography)\n"
            "- semi_dynamic: Facts that change occasionally over months/years "
            "(e.g., job, residence, relationships)\n"
            "- dynamic: Facts that change frequently or are time-sensitive "
            "(e.g., current status, today's events, prices)\n\n"
            "Respond with ONLY a JSON object:\n"
            '{"stability": "static|semi_dynamic|dynamic", '
            '"confidence": 0.0-1.0, '
            '"ttl_days": null|integer, '
            '"reasoning": "brief explanation"}'
        )

        import json

        # Call LLM provider
        response = self.llm_provider.generate(prompt)
        response_text = response if isinstance(response, str) else str(response)

        # Parse JSON response
        # Try to find JSON in the response
        json_match = re.search(r"\{[^}]+\}", response_text)
        if not json_match:
            raise ValueError(f"No JSON found in LLM response: {response_text[:200]}")

        parsed = json.loads(json_match.group())

        stability = parsed.get("stability", "semi_dynamic")
        if stability not in VALID_STABILITIES:
            stability = "semi_dynamic"

        confidence = float(parsed.get("confidence", 0.7))
        confidence = max(0.0, min(1.0, confidence))

        ttl_days = parsed.get("ttl_days")
        if ttl_days is not None:
            ttl_days = int(ttl_days)

        return StabilityClassification(
            temporal_stability=stability,
            confidence=round(confidence, 2),
            estimated_ttl_days=ttl_days,
            method="llm",
            signals=[f"llm_reasoning:{parsed.get('reasoning', 'n/a')[:100]}"],
        )
