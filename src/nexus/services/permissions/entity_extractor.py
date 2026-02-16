"""Backward-compat shim: nexus.services.permissions.entity_extractor.

Canonical location: ``nexus.rebac.entity_extractor``
"""

from nexus.rebac.entity_extractor import (
    EntityExtractor,
    EntityType,
    ExtractedEntity,
    extract_entities,
    extract_entities_as_dicts,
    get_default_extractor,
)

__all__ = [
    "EntityExtractor",
    "EntityType",
    "ExtractedEntity",
    "extract_entities",
    "extract_entities_as_dicts",
    "get_default_extractor",
]
