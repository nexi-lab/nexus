"""Readme error formatter — extracted from ReadmeDocMixin, ValidatedMixin, TraitBasedMixin.

Centralizes all error-with-readme-reference formatting (DRY #5-A).
``format_trait_error`` merged into ``format_error`` (Issue #2086, 6A).
"""

import posixpath

from nexus.backends.connectors.base import ErrorDef, ValidationError


class ReadmeErrorFormatter:
    """Format connector errors with README.md references.

    Parameters
    ----------
    skill_name:
        Skill identifier (e.g., ``"gcalendar"``).
    mount_path:
        Mount path for building readme_md_path references.
    """

    def __init__(self, skill_name: str, mount_path: str = "") -> None:
        self._skill_name = skill_name
        self._mount_path = mount_path

    @property
    def readme_md_path(self) -> str:
        """Get path to README.md (for error messages)."""
        if self._mount_path:
            return posixpath.join(self._mount_path.rstrip("/"), ".readme", "README.md")
        return "/.readme/README.md"

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
            section: README.md section anchor (optional, overridden by registry).
            fix_example: Example fix (optional, overridden by registry).

        Returns:
            ValidationError with skill reference.
        """
        registry = error_registry or {}
        error_def = registry.get(code)
        if error_def:
            section = section or error_def.readme_section
            fix_example = fix_example or error_def.fix_example
            message = message or error_def.message

        return ValidationError(
            code=code,
            message=message,
            readme_path=self.readme_md_path,
            readme_section=section,
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
        """Format a trait validation error.

        Unlike ``format_error``, the registry takes priority here because
        the *section* and *fix* values are generic defaults from
        ``validate_traits()``, while the registry contains domain-specific
        values.
        """
        registry = error_registry or {}
        error_def = registry.get(code)
        resolved_section = (error_def.readme_section if error_def else None) or section
        resolved_fix = (error_def.fix_example if error_def else None) or fix
        resolved_message = message or (error_def.message if error_def else message)
        return self.format_error(
            code=code,
            message=resolved_message,
            section=resolved_section,
            fix_example=resolved_fix,
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
            readme_path=self.readme_md_path,
            readme_section=operation.replace("_", "-"),
            field_errors=field_errors,
        )
