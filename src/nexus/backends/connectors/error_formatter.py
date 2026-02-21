"""Skill error formatter — extracted from SkillDocMixin, ValidatedMixin, TraitBasedMixin.

Centralizes all error-with-skill-reference formatting (DRY #5-A).
``format_trait_error`` merged into ``format_error`` (Issue #2086, 6A).
"""

from __future__ import annotations

import posixpath

from nexus.backends.connectors.base import ErrorDef, ValidationError


class SkillErrorFormatter:
    """Format connector errors with SKILL.md references.

    Parameters
    ----------
    skill_name:
        Skill identifier (e.g., ``"gcalendar"``).
    mount_path:
        Mount path for building skill_md_path references.
    """

    def __init__(self, skill_name: str, mount_path: str = "") -> None:
        self._skill_name = skill_name
        self._mount_path = mount_path

    @property
    def skill_md_path(self) -> str:
        """Get path to SKILL.md (for error messages)."""
        if self._mount_path:
            return posixpath.join(self._mount_path.rstrip("/"), ".skill", "SKILL.md")
        return "/.skill/SKILL.md"

    def format_error(
        self,
        code: str,
        message: str,
        error_registry: dict[str, ErrorDef] | None = None,
        section: str | None = None,
        fix_example: str | None = None,
    ) -> ValidationError:
        """Create ValidationError with skill reference.

        Unified method that replaces both ``format_error_with_skill_ref``
        and ``format_trait_error``.  When *message* is non-empty it is used
        as-is; when empty the registry message (if any) is used instead.

        Args:
            code: Error code (e.g., ``"MISSING_AGENT_INTENT"``).
            message: Error message (empty string falls back to registry).
            error_registry: Registry to look up error definitions.
            section: SKILL.md section anchor (optional, overridden by registry).
            fix_example: Example fix (optional, overridden by registry).

        Returns:
            ValidationError with skill reference.
        """
        registry = error_registry or {}
        error_def = registry.get(code)
        if error_def:
            section = section or error_def.skill_section
            fix_example = fix_example or error_def.fix_example
            message = message or error_def.message

        return ValidationError(
            code=code,
            message=message,
            skill_path=self.skill_md_path,
            skill_section=section,
            fix_example=fix_example,
        )

    # Keep old names as thin aliases for backward compatibility.
    def format_error_with_skill_ref(
        self,
        code: str,
        message: str,
        error_registry: dict[str, ErrorDef] | None = None,
        section: str | None = None,
        fix_example: str | None = None,
    ) -> ValidationError:
        """Alias for ``format_error`` (backward compat)."""
        return self.format_error(
            code=code,
            message=message,
            error_registry=error_registry,
            section=section,
            fix_example=fix_example,
        )

    def format_trait_error(
        self,
        code: str,
        message: str,
        section: str,
        fix: str,
        error_registry: dict[str, ErrorDef] | None = None,
    ) -> ValidationError:
        """Alias for ``format_error`` (backward compat).

        Unlike ``format_error``, the *section* and *fix* params here are
        **fallback defaults** — the registry takes priority when present.
        """
        registry = error_registry or {}
        error_def = registry.get(code)
        if error_def:
            section = error_def.skill_section or section
            fix = error_def.fix_example or fix
            message = message or error_def.message

        return ValidationError(
            code=code,
            message=message,
            skill_path=self.skill_md_path,
            skill_section=section,
            fix_example=fix,
        )

    def format_validation_error(
        self,
        operation: str,
        field_errors: dict[str, str],
    ) -> ValidationError:
        """Create ValidationError for schema validation failures.

        Args:
            operation: Operation name (e.g., ``"create_event"``).
            field_errors: Field-level error messages.

        Returns:
            ValidationError with field details and skill reference.
        """
        return ValidationError(
            code="SCHEMA_VALIDATION_ERROR",
            message=f"Invalid {operation} data",
            skill_path=self.skill_md_path,
            skill_section=operation.replace("_", "-"),
            field_errors=field_errors,
        )
