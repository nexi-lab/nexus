"""Connector framework with validation mixins.

This module provides a layered, opt-in framework for building connectors
with agent-friendly validation and self-correcting error messages.

Mixins (in order of complexity):
- SkillDocMixin: SKILL.md integration with SkillRegistry
- ValidatedMixin: Pydantic schema validation
- TraitBasedMixin: Operation traits (reversibility, confirmation levels)
- CheckpointMixin: Rollback support for reversible operations

Example:
    >>> class MyConnector(Backend, CacheConnectorMixin, SkillDocMixin, ValidatedMixin):
    ...     SKILL_NAME = "myconnector"
    ...     SCHEMAS = {"create": CreateSchema}
"""

from nexus.backends.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    Reversibility,
    SkillDocMixin,
    TraitBasedMixin,
    ValidatedMixin,
    ValidationError,
)
from nexus.backends.connectors.error_formatter import SkillErrorFormatter
from nexus.backends.connectors.schema_generator import SkillDocGenerator

__all__ = [
    "Reversibility",
    "ConfirmLevel",
    "OpTraits",
    "ErrorDef",
    "ValidationError",
    "SkillDocMixin",
    "ValidatedMixin",
    "TraitBasedMixin",
    "CheckpointMixin",
    "SkillDocGenerator",
    "SkillErrorFormatter",
]
