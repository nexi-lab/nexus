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

from nexus.connectors.base import (
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
from nexus.connectors.mount_hooks import generate_all_skill_docs, on_mount

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
    "on_mount",
    "generate_all_skill_docs",
]
