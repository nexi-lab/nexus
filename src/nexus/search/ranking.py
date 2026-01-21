"""Attribute-based ranking for search results.

Implements field weighting to boost matches in important fields (filename, title)
over matches in less important fields (content body).

Industry references:
- Meilisearch: Position-based attribute ranking
- Elasticsearch: BM25F with field boosts
- Typesense: Explicit query_by_weights

Issue: #1092
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AttributeWeights:
    """Configurable weights for different document fields.

    Higher weights mean matches in that field are considered more relevant.
    All weights are multipliers applied to the base score.

    Example:
        >>> weights = AttributeWeights(filename=3.0, content=1.0)
        >>> # A filename match will score 3x higher than a content match
    """

    # Field weights (multipliers)
    filename: float = 3.0  # Filename matches are highly relevant
    title: float = 2.5  # Title/header matches
    path: float = 2.0  # Path component matches
    tags: float = 2.0  # Tag matches
    description: float = 1.5  # Description/summary
    content: float = 1.0  # Body content (baseline)

    # Exactness bonuses
    exact_match_boost: float = 1.5  # Multiplier for exact phrase matches
    prefix_match_boost: float = (
        1.2  # Multiplier for prefix matches (e.g., "auth" matches "authentication")
    )

    def get_weight(self, field_name: str) -> float:
        """Get weight for a field, defaulting to 1.0 for unknown fields.

        Args:
            field_name: Name of the field

        Returns:
            Weight multiplier for the field
        """
        return getattr(self, field_name, 1.0)


@dataclass
class RankingConfig:
    """Full ranking configuration for search results.

    Attributes:
        attribute_weights: Per-field weight configuration
        enable_attribute_boosting: Whether to apply field-based boosting
        enable_exactness_boost: Whether to apply exact match bonuses
    """

    attribute_weights: AttributeWeights = field(default_factory=AttributeWeights)
    enable_attribute_boosting: bool = True
    enable_exactness_boost: bool = True


def get_ranking_config_from_env() -> RankingConfig:
    """Load ranking configuration from environment variables.

    Environment variables:
        NEXUS_SEARCH_WEIGHT_FILENAME: Weight for filename matches (default: 3.0)
        NEXUS_SEARCH_WEIGHT_TITLE: Weight for title matches (default: 2.5)
        NEXUS_SEARCH_WEIGHT_PATH: Weight for path matches (default: 2.0)
        NEXUS_SEARCH_WEIGHT_TAGS: Weight for tag matches (default: 2.0)
        NEXUS_SEARCH_WEIGHT_DESCRIPTION: Weight for description matches (default: 1.5)
        NEXUS_SEARCH_WEIGHT_CONTENT: Weight for content matches (default: 1.0)
        NEXUS_SEARCH_EXACT_MATCH_BOOST: Bonus for exact matches (default: 1.5)
        NEXUS_SEARCH_ATTRIBUTE_BOOST: Enable attribute boosting (default: true)
        NEXUS_SEARCH_EXACTNESS_BOOST: Enable exactness boosting (default: true)

    Returns:
        RankingConfig loaded from environment
    """
    return RankingConfig(
        attribute_weights=AttributeWeights(
            filename=float(os.environ.get("NEXUS_SEARCH_WEIGHT_FILENAME", "3.0")),
            title=float(os.environ.get("NEXUS_SEARCH_WEIGHT_TITLE", "2.5")),
            path=float(os.environ.get("NEXUS_SEARCH_WEIGHT_PATH", "2.0")),
            tags=float(os.environ.get("NEXUS_SEARCH_WEIGHT_TAGS", "2.0")),
            description=float(os.environ.get("NEXUS_SEARCH_WEIGHT_DESCRIPTION", "1.5")),
            content=float(os.environ.get("NEXUS_SEARCH_WEIGHT_CONTENT", "1.0")),
            exact_match_boost=float(os.environ.get("NEXUS_SEARCH_EXACT_MATCH_BOOST", "1.5")),
            prefix_match_boost=float(os.environ.get("NEXUS_SEARCH_PREFIX_MATCH_BOOST", "1.2")),
        ),
        enable_attribute_boosting=os.environ.get("NEXUS_SEARCH_ATTRIBUTE_BOOST", "true").lower()
        == "true",
        enable_exactness_boost=os.environ.get("NEXUS_SEARCH_EXACTNESS_BOOST", "true").lower()
        == "true",
    )


def detect_matched_field(
    query: str,
    path: str,
    content: str | None = None,  # noqa: ARG001 - kept for API consistency
    title: str | None = None,
    tags: list[str] | None = None,
    description: str | None = None,
) -> str:
    """Detect which field the query primarily matched in.

    Checks fields in order of importance (filename first, content last)
    and returns the first field where a match is found.

    Args:
        query: Search query
        path: File path
        content: File content (optional)
        title: Document title (optional)
        tags: Document tags (optional)
        description: Document description (optional)

    Returns:
        Name of the matched field ("filename", "title", "path", "tags", "description", "content")
    """
    query_lower = query.lower().strip()
    query_terms = query_lower.split()

    # Extract filename from path
    filename = path.split("/")[-1].lower() if path else ""
    filename_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Check filename (highest priority)
    if query_lower in filename or query_lower in filename_without_ext:
        return "filename"

    # Check if all query terms appear in filename
    if all(term in filename for term in query_terms):
        return "filename"

    # Check title
    if title:
        title_lower = title.lower()
        if query_lower in title_lower or all(term in title_lower for term in query_terms):
            return "title"

    # Check tags
    if tags:
        tags_lower = [t.lower() for t in tags]
        tags_combined = " ".join(tags_lower)
        if query_lower in tags_combined or any(query_lower in t for t in tags_lower):
            return "tags"

    # Check path (excluding filename)
    path_lower = path.lower() if path else ""
    path_without_filename = "/".join(path_lower.split("/")[:-1]) if "/" in path_lower else ""
    if query_lower in path_without_filename:
        return "path"

    # Check description
    if description:
        desc_lower = description.lower()
        if query_lower in desc_lower:
            return "description"

    # Default to content
    return "content"


def check_exact_match(query: str, text: str) -> bool:
    """Check if query appears as an exact phrase in text.

    Args:
        query: Search query
        text: Text to check

    Returns:
        True if exact phrase match found
    """
    if not query or not text:
        return False

    query_lower = query.lower().strip()
    text_lower = text.lower()

    # Check for exact phrase match (with word boundaries)
    pattern = r"\b" + re.escape(query_lower) + r"\b"
    return bool(re.search(pattern, text_lower))


def check_prefix_match(query: str, text: str) -> bool:
    """Check if query is a prefix of any word in text.

    Args:
        query: Search query
        text: Text to check

    Returns:
        True if prefix match found
    """
    if not query or not text:
        return False

    query_lower = query.lower().strip()
    text_lower = text.lower()

    # Check if query is a prefix of any word
    words = re.findall(r"\b\w+", text_lower)
    return any(word.startswith(query_lower) for word in words)


def apply_attribute_boosting(
    results: list[dict[str, Any]],
    query: str,
    config: RankingConfig | None = None,
) -> list[dict[str, Any]]:
    """Apply attribute-based score boosting to search results.

    Boosts scores based on:
    1. Which field the query matched in (filename > title > path > content)
    2. Whether the match is exact or partial

    Args:
        results: Search results with 'path', 'score', and optionally 'chunk_text' fields
        query: Original search query
        config: Ranking configuration (uses defaults if not provided)

    Returns:
        Re-ranked results with boosted scores, sorted by new score descending

    Example:
        >>> results = [
        ...     {"path": "/docs/readme.md", "score": 0.8, "chunk_text": "..."},
        ...     {"path": "/src/auth.py", "score": 0.7, "chunk_text": "authentication..."},
        ... ]
        >>> boosted = apply_attribute_boosting(results, "auth")
        >>> # auth.py now ranks higher due to filename match
    """
    if config is None:
        config = RankingConfig()

    if not config.enable_attribute_boosting:
        return results

    if not results:
        return results

    query_stripped = query.strip()
    if not query_stripped:
        return results

    boosted_results = []

    for result in results:
        # Create a copy to avoid mutating original
        boosted = result.copy()
        original_score = boosted.get("score", 0.0)

        # Extract fields for matching
        path = boosted.get("path", "")
        chunk_text = boosted.get("chunk_text", "")
        title = boosted.get("title")
        tags = boosted.get("tags")
        description = boosted.get("description")

        # Detect which field matched
        matched_field = boosted.get("matched_field")
        if not matched_field:
            matched_field = detect_matched_field(
                query_stripped,
                path,
                content=chunk_text,
                title=title,
                tags=tags,
                description=description,
            )

        # Apply field weight
        field_weight = config.attribute_weights.get_weight(matched_field)
        boost = field_weight

        # Apply exactness boost
        is_exact_match = False
        is_prefix_match = False

        if config.enable_exactness_boost:
            # Check for exact match in the relevant text
            text_to_check = chunk_text
            if matched_field == "filename":
                text_to_check = path.split("/")[-1] if path else ""
            elif matched_field == "title" and title:
                text_to_check = title
            elif matched_field == "path":
                text_to_check = path
            elif matched_field == "description" and description:
                text_to_check = description

            if check_exact_match(query_stripped, text_to_check):
                boost *= config.attribute_weights.exact_match_boost
                is_exact_match = True
            elif check_prefix_match(query_stripped, text_to_check):
                boost *= config.attribute_weights.prefix_match_boost
                is_prefix_match = True

        # Apply boost to score
        boosted["score"] = original_score * boost
        boosted["matched_field"] = matched_field
        boosted["attribute_boost"] = boost
        boosted["original_score"] = original_score

        if is_exact_match:
            boosted["is_exact_match"] = True
        if is_prefix_match:
            boosted["is_prefix_match"] = True

        boosted_results.append(boosted)

    # Sort by boosted score (descending)
    boosted_results.sort(key=lambda x: x.get("score", 0.0), reverse=True)

    logger.debug(
        f"[RANKING] Applied attribute boosting to {len(boosted_results)} results for query '{query_stripped}'"
    )

    return boosted_results
