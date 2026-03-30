"""Connector framework with validation mixins.

This module provides a layered, opt-in framework for building connectors
with agent-friendly validation and self-correcting error messages.

Mixins (in order of complexity):
- ReadmeDocMixin: README.md integration with SkillRegistry
- ValidatedMixin: Pydantic schema validation
- TraitBasedMixin: Operation traits (reversibility, confirmation levels)
- CheckpointMixin: Rollback support for reversible operations

Example:
    >>> class MyConnector(Backend, CacheConnectorMixin, ReadmeDocMixin, ValidatedMixin):
    ...     SKILL_NAME = "myconnector"
    ...     SCHEMAS = {"create": CreateSchema}
"""

from nexus.backends.connectors.base import (
    CheckpointMixin,
    ConfirmLevel,
    ErrorDef,
    OpTraits,
    ReadmeDocMixin,
    Reversibility,
    TraitBasedMixin,
    ValidatedMixin,
    ValidationError,
)
from nexus.backends.connectors.error_formatter import ReadmeErrorFormatter
from nexus.backends.connectors.schema_generator import ReadmeDocGenerator

__all__ = [
    "Reversibility",
    "ConfirmLevel",
    "OpTraits",
    "ErrorDef",
    "ValidationError",
    "ReadmeDocMixin",
    "ValidatedMixin",
    "TraitBasedMixin",
    "CheckpointMixin",
    "ReadmeDocGenerator",
    "ReadmeErrorFormatter",
]
