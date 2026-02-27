"""Local exception types for the Skills module.

These exceptions decouple the skills module from nexus.core.exceptions,
allowing the skills module to be tested and used independently.

Each exception mirrors the semantics of its nexus.core counterpart
but lives locally within the skills module boundary.
"""

from __future__ import annotations


class SkillValidationError(Exception):
    """Raised when skill validation fails (invalid input, bad format)."""

    is_expected = True

    def __init__(self, message: str, path: str | None = None):
        self.message = message
        self.path = path
        super().__init__(message)


class SkillPermissionDeniedError(Exception):
    """Raised when a skill operation is denied due to insufficient permissions."""

    is_expected = True

    def __init__(self, message: str, path: str | None = None):
        self.message = message
        self.path = path
        super().__init__(message)


class SkillNotFoundError(SkillValidationError):
    """Raised when a skill is not found in the registry."""

    pass


class SkillDependencyError(SkillValidationError):
    """Raised when skill dependencies cannot be resolved."""

    pass


class SkillManagerError(SkillValidationError):
    """Raised when skill management operations fail."""

    pass


class SkillExportError(SkillValidationError):
    """Raised when skill export fails."""

    pass


class SkillParseError(SkillValidationError):
    """Raised when parsing a SKILL.md file fails."""

    pass


class SkillImportError(SkillValidationError):
    """Raised when skill import fails."""

    pass
