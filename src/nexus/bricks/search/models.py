"""Search brick model re-exports (Issue #1520).

Re-exports ORM models from nexus.storage.models for brick-internal use.
This avoids direct imports from nexus.storage.models scattered across
search modules, centralizing the dependency in one place.

Allowed by brick lint — nexus.storage.* is in the allowed import list
per .pre-commit-hooks/check_brick_imports.py:26.
"""

from nexus.storage.models import (
    DocumentChunkModel,
    EntityMentionModel,
    EntityModel,
    FilePathModel,
    RelationshipModel,
)

__all__ = [
    "DocumentChunkModel",
    "EntityMentionModel",
    "EntityModel",
    "FilePathModel",
    "RelationshipModel",
]
