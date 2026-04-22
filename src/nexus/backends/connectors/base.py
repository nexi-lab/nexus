"""Base mixins for connector validation framework.

This module provides opt-in mixins that connectors can use to add:
- README.md documentation (auto-generated)
- Pydantic schema validation
- Operation traits (reversibility, confirmation levels)
- Checkpoint/rollback support

Each connector configures these mixins via class attributes.
"""

import logging
import posixpath
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from nexus.contracts.exceptions import ValidationError as CoreValidationError

if TYPE_CHECKING:
    from typing import Protocol as _Protocol

    from nexus.backends.connectors.error_formatter import ReadmeErrorFormatter
    from nexus.backends.connectors.schema_generator import ReadmeDocGenerator

    class SkillRegistryProtocol(_Protocol):
        """Stub protocol (skills brick removed)."""

        ...


logger = logging.getLogger(__name__)

# =============================================================================
# Enums & Data Classes
# =============================================================================


class Reversibility(StrEnum):
    """How reversible an operation is."""

    FULL = "full"  # Can undo completely (e.g., delete created event)
    PARTIAL = "partial"  # Can undo with limitations (e.g., restore from trash)
    NONE = "none"  # Cannot undo (e.g., send email)


_CONFIRM_LEVEL_ORDER: dict[str, int] = {"none": 0, "intent": 1, "explicit": 2, "user": 3}


class ConfirmLevel(StrEnum):
    """Required confirmation level for an operation.

    Levels in order of increasing strictness:
    - NONE (0): No confirmation needed
    - INTENT (1): Requires agent_intent comment
    - EXPLICIT (2): Requires intent + confirm: true
    - USER (3): Must ask user for confirmation
    """

    NONE = "none"  # No confirmation needed
    INTENT = "intent"  # Requires agent_intent comment
    EXPLICIT = "explicit"  # Requires intent + confirm: true
    USER = "user"  # Must ask user for confirmation

    @property
    def level(self) -> int:
        """Return numeric level for comparison."""
        return _CONFIRM_LEVEL_ORDER[self.value]

    def __ge__(self, other: object) -> bool:
        if isinstance(other, ConfirmLevel):
            return self.level >= other.level
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, ConfirmLevel):
            return self.level < other.level
        return NotImplemented


@dataclass
class OpTraits:
    """Operation traits defining behavior and requirements.

    Connectors define these per operation (create, update, delete).

    Example:
        >>> OPERATION_TRAITS = {
        ...     "create_event": OpTraits(
        ...         reversibility=Reversibility.FULL,
        ...         confirm=ConfirmLevel.INTENT,
        ...     ),
        ...     "send_email": OpTraits(
        ...         reversibility=Reversibility.NONE,
        ...         confirm=ConfirmLevel.USER,
        ...         checkpoint=False,
        ...         warnings=["THIS ACTION CANNOT BE UNDONE"],
        ...     ),
        ... }
    """

    reversibility: Reversibility = Reversibility.FULL
    confirm: ConfirmLevel = ConfirmLevel.INTENT
    checkpoint: bool = True
    intent_min_length: int = 10
    warnings: list[str] = field(default_factory=list)


@dataclass
class ErrorDef:
    """Error definition with self-correcting information.

    Used in ERROR_REGISTRY to provide agent-friendly error messages
    that include fix examples and README.md references.

    Example:
        >>> ERROR_REGISTRY = {
        ...     "MISSING_AGENT_INTENT": ErrorDef(
        ...         message="Operations require agent_intent",
        ...         readme_section="required-format",
        ...         fix_example="# agent_intent: User requested meeting",
        ...     ),
        ... }
    """

    message: str
    readme_section: str  # README.md section anchor
    fix_example: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class ValidationError(CoreValidationError):
    """Validation error with self-correcting information.

    Inherits from core.exceptions.ValidationError so centralized error handlers
    can catch both connector and core validation errors uniformly.

    Contains error code, message, skill reference, and fix example
    so agents can self-correct their requests.
    """

    def __init__(
        self,
        code: str,
        message: str,
        readme_path: str | None = None,
        readme_section: str | None = None,
        fix_example: str | None = None,
        field_errors: dict[str, str] | None = None,
    ):
        self.code = code
        self.message = message
        self.readme_path = readme_path
        self.readme_section = readme_section
        self.fix_example = fix_example
        self.field_errors = field_errors or {}
        super().__init__(self.format_message())

    def format_message(self) -> str:
        """Format error message with skill reference and fix example."""
        lines = [f"[{self.code}] {self.message}"]

        if self.field_errors:
            lines.append("\nField errors:")
            for field_name, error in self.field_errors.items():
                lines.append(f"  - {field_name}: {error}")

        if self.readme_path:
            ref = self.readme_path
            if self.readme_section:
                ref += f"#{self.readme_section}"
            lines.append(f"\nSee: {ref}")

        if self.fix_example:
            lines.append(f"\nFix:\n```yaml\n{self.fix_example}\n```")

        return "\n".join(lines)


# =============================================================================
# ReadmeDocMixin - README.md Integration
# =============================================================================


class ReadmeDocMixin:
    """Mixin for README.md integration with auto-generation.

    Connectors configure:
        SKILL_NAME: str - Skill identifier (e.g., "gcalendar")
        README_DIR: str - Directory name for skill docs (default: ".readme")

    Features:
        - Auto-generates .readme/ directory with README.md and examples
        - Integrates with SkillRegistry for discovery
        - Formats errors with readme references

    Delegates heavy lifting to ``ReadmeDocGenerator`` and ``ReadmeErrorFormatter``.
    """

    SKILL_NAME: str = ""
    README_DIR: str = ".readme"  # Directory at mount path

    # Subclasses provide these (used for auto-generation)
    SCHEMAS: dict[str, type[BaseModel]] = {}
    OPERATION_TRAITS: dict[str, OpTraits] = {}
    ERROR_REGISTRY: dict[str, ErrorDef] = {}
    EXAMPLES: dict[str, str] = {}  # Example files: {"create_meeting.yaml": "content..."}
    NESTED_EXAMPLES: dict[str, list[str]] = {}  # Nested field examples for README.md
    FIELD_EXAMPLES: dict[str, str] = {}  # Field-specific examples for README.md

    _skill_registry: "SkillRegistryProtocol | None" = None
    _mount_path: str | None = None  # Set during mount
    _cached_doc_generator: "ReadmeDocGenerator | None" = None
    _cached_error_formatter: "ReadmeErrorFormatter | None" = None

    @property
    def readme_md_path(self) -> str:
        """Get path to README.md (for error messages)."""
        if self._mount_path:
            return posixpath.join(self._mount_path.rstrip("/"), self.README_DIR, "README.md")
        return "/.readme/README.md"  # Default fallback

    def set_skill_registry(self, registry: "SkillRegistryProtocol") -> None:
        """Set the skill registry for this connector."""
        self._skill_registry = registry

    def set_mount_path(self, mount_path: str) -> None:
        """Set the mount path (called during mount).

        Invalidates cached delegates since mount_path changed.
        """
        self._mount_path = mount_path
        self._cached_doc_generator = None
        self._cached_error_formatter = None

    def get_doc_generator(self) -> "ReadmeDocGenerator":
        """Get or create the cached ReadmeDocGenerator."""
        if self._cached_doc_generator is None:
            from nexus.backends.connectors.schema_generator import ReadmeDocGenerator

            # Extract write paths from CLIConnectorConfig if available
            write_paths: dict[str, str] = {}
            _config = getattr(self, "_config", None)
            if _config and hasattr(_config, "write"):
                for wp in _config.write:
                    write_paths[wp.operation] = wp.path

            self._cached_doc_generator = ReadmeDocGenerator(
                skill_name=self.SKILL_NAME,
                schemas=self.SCHEMAS,
                operation_traits=self.OPERATION_TRAITS,
                error_registry=self.ERROR_REGISTRY,
                examples=self.EXAMPLES,
                readme_dir=self.README_DIR,
                nested_examples=self.NESTED_EXAMPLES or None,
                field_examples=self.FIELD_EXAMPLES or None,
                write_paths=write_paths or None,
            )
            # Set directory structure if connector defines it
            dir_structure = getattr(self, "DIRECTORY_STRUCTURE", None)
            if dir_structure:
                self._cached_doc_generator._directory_structure = dir_structure
        return self._cached_doc_generator

    def _get_error_formatter(self) -> "ReadmeErrorFormatter":
        """Get or create the cached ReadmeErrorFormatter."""
        if self._cached_error_formatter is None:
            from nexus.backends.connectors.error_formatter import ReadmeErrorFormatter

            self._cached_error_formatter = ReadmeErrorFormatter(
                skill_name=self.SKILL_NAME,
                mount_path=self._mount_path or "",
            )
        return self._cached_error_formatter

    def generate_readme(self, mount_path: str) -> str:
        """Auto-generate README.md from connector metadata."""
        return self.get_doc_generator().generate_readme(mount_path)

    def get_readme_path(self, mount_path: str) -> str:
        """Get the full path to the .readme directory."""
        return self.get_doc_generator().get_readme_path(mount_path)

    # NOTE (Issue #3728): ``write_readme`` was removed. The virtual
    # ``.readme/`` overlay now serves docs on-demand from class metadata
    # via ``nexus.backends.connectors.schema_generator.dispatch_virtual_readme_*``,
    # so materializing files into the backend is no longer needed and
    # would drift from the canonical (class-metadata-derived) content.

    def format_error_with_skill_ref(
        self,
        code: str,
        message: str,
        section: str | None = None,
        fix_example: str | None = None,
    ) -> ValidationError:
        """Create ValidationError with skill reference."""
        return self._get_error_formatter().format_error(
            code=code,
            message=message,
            error_registry=self.ERROR_REGISTRY,
            section=section,
            fix_example=fix_example,
        )


# =============================================================================
# ValidatedMixin - Pydantic Schema Validation
# =============================================================================


class ValidatedMixin:
    """Mixin for Pydantic schema validation.

    Connectors configure:
        SCHEMAS: dict[str, type[BaseModel]] - Operation name -> Pydantic model

    Example:
        >>> class MyConnector(Backend, ValidatedMixin):
        ...     SCHEMAS = {
        ...         "create_event": CreateEventSchema,
        ...         "update_event": UpdateEventSchema,
        ...     }
    """

    SCHEMAS: dict[str, type[BaseModel]] = {}

    def validate_schema(self, operation: str, data: dict[str, Any]) -> BaseModel:
        """Validate data against schema for operation.

        Args:
            operation: Operation name (e.g., "create_event")
            data: Data to validate

        Returns:
            Validated Pydantic model

        Raises:
            ValidationError: If validation fails
        """
        schema = self.SCHEMAS.get(operation)
        if not schema:
            # No schema defined - skip validation
            return cast("BaseModel", data)

        try:
            return schema.model_validate(data)
        except PydanticValidationError as e:
            field_errors = {}
            for error in e.errors():
                loc = ".".join(str(x) for x in error["loc"])
                field_errors[loc] = error["msg"]

            # Reuse cached formatter from ReadmeDocMixin if available
            formatter_fn = getattr(self, "_get_error_formatter", None)
            if formatter_fn is not None:
                formatter = formatter_fn()
            else:
                from nexus.backends.connectors.error_formatter import ReadmeErrorFormatter

                skill_name = getattr(self, "SKILL_NAME", "")
                mount_path = getattr(self, "_mount_path", "") or ""
                formatter = ReadmeErrorFormatter(skill_name=skill_name, mount_path=mount_path)
            raise formatter.format_validation_error(operation, field_errors) from e


# =============================================================================
# TraitBasedMixin - Operation Traits Validation
# =============================================================================


class TraitBasedMixin:
    """Mixin for operation trait validation.

    Connectors configure:
        OPERATION_TRAITS: dict[str, OpTraits] - Operation name -> traits

    Validates:
        - agent_intent presence and length
        - confirm flag for explicit operations
        - user_confirmed for irreversible operations
    """

    OPERATION_TRAITS: dict[str, OpTraits] = {}
    ERROR_REGISTRY: dict[str, ErrorDef] = {}

    def validate_traits(self, operation: str, data: dict[str, Any]) -> list[str]:
        """Validate operation traits.

        Args:
            operation: Operation name (e.g., "create_event")
            data: Request data (should include agent_intent, confirm, etc.)

        Returns:
            List of warnings (empty if none)

        Raises:
            ValidationError: If trait requirements not met
        """
        traits = self.OPERATION_TRAITS.get(operation)
        if not traits:
            return []

        warnings = []

        # Check agent_intent
        if traits.confirm >= ConfirmLevel.INTENT:
            agent_intent = data.get("agent_intent", "")
            if not agent_intent:
                raise self._trait_error(
                    code="MISSING_AGENT_INTENT",
                    message=f"Operation '{operation}' requires agent_intent",
                    section="required-format",
                    fix="# agent_intent: <reason for this operation>",
                )

            if len(agent_intent) < traits.intent_min_length:
                raise self._trait_error(
                    code="AGENT_INTENT_TOO_SHORT",
                    message=f"agent_intent must be at least {traits.intent_min_length} characters",
                    section="required-format",
                    fix=f"# agent_intent: <provide at least {traits.intent_min_length} characters>",
                )

        # Check explicit confirmation
        if traits.confirm >= ConfirmLevel.EXPLICIT and not data.get("confirm"):
            raise self._trait_error(
                code="MISSING_CONFIRM",
                message=f"Operation '{operation}' requires explicit confirmation",
                section="required-format",
                fix="# confirm: true",
            )

        # Check user confirmation
        if traits.confirm == ConfirmLevel.USER and not data.get("user_confirmed"):
            raise self._trait_error(
                code="MISSING_USER_CONFIRMATION",
                message=f"Operation '{operation}' requires user confirmation. "
                "This action CANNOT be undone. Ask user first.",
                section="irreversible-operations",
                fix="# user_confirmed: true  # Only after explicit user approval",
            )

        # Collect warnings
        warnings.extend(traits.warnings)

        return warnings

    def _trait_error(self, code: str, message: str, section: str, fix: str) -> ValidationError:
        """Create ValidationError for trait validation failure."""
        from nexus.backends.connectors.error_formatter import ReadmeErrorFormatter

        skill_name = getattr(self, "SKILL_NAME", "")
        mount_path = getattr(self, "_mount_path", "") or ""
        formatter = ReadmeErrorFormatter(skill_name=skill_name, mount_path=mount_path)
        return formatter.format_trait_error(
            code=code,
            message=message,
            section=section,
            fix=fix,
            error_registry=self.ERROR_REGISTRY,
        )

    def get_operation_traits(self, operation: str) -> OpTraits | None:
        """Get traits for an operation."""
        return self.OPERATION_TRAITS.get(operation)


# =============================================================================
# CheckpointMixin - Rollback Support
# =============================================================================


@dataclass
class Checkpoint:
    """Checkpoint for rollback support.

    Stores state before an operation so it can be reverted.
    """

    checkpoint_id: str
    operation: str
    timestamp: str
    previous_state: dict[str, Any] | None
    created_state: dict[str, Any] | None
    metadata: dict[str, Any] = field(default_factory=dict)


class CheckpointMixin:
    """Mixin for checkpoint/rollback support.

    Provides:
        - create_checkpoint(): Store state before operation
        - rollback(): Revert to previous state
        - get_checkpoint(): Retrieve checkpoint by ID

    Only creates checkpoints for operations where traits.checkpoint=True.
    """

    OPERATION_TRAITS: dict[str, OpTraits] = {}

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Instance-level checkpoint storage (fix for shared-dict bug #7-A)
        self._checkpoints: dict[str, Checkpoint] = {}

    def create_checkpoint(
        self,
        operation: str,
        previous_state: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint | None:
        """Create checkpoint before operation.

        Args:
            operation: Operation name
            previous_state: State before operation (for updates/deletes)
            metadata: Additional metadata

        Returns:
            Checkpoint if created, None if operation doesn't support checkpoints
        """
        import uuid
        from datetime import UTC, datetime

        # Check if operation supports checkpoints
        traits = self.OPERATION_TRAITS.get(operation)
        if not traits or not traits.checkpoint:
            return None

        checkpoint = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            operation=operation,
            timestamp=datetime.now(UTC).isoformat(),
            previous_state=previous_state,
            created_state=None,  # Set after operation completes
            metadata=metadata or {},
        )

        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        logger.debug(f"Created checkpoint {checkpoint.checkpoint_id} for {operation}")

        return checkpoint

    def complete_checkpoint(self, checkpoint_id: str, created_state: dict[str, Any]) -> None:
        """Mark checkpoint complete with created state.

        Args:
            checkpoint_id: Checkpoint ID
            created_state: State after operation (for creates)
        """
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint:
            checkpoint.created_state = created_state
            logger.debug(f"Completed checkpoint {checkpoint_id}")

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        """Get checkpoint by ID."""
        return self._checkpoints.get(checkpoint_id)

    def rollback(self, checkpoint_id: str) -> dict[str, Any]:
        """Rollback to checkpoint state.

        Args:
            checkpoint_id: Checkpoint ID

        Returns:
            Rollback result with action taken

        Raises:
            ValidationError: If checkpoint not found or rollback not possible
        """
        checkpoint = self._checkpoints.get(checkpoint_id)
        if not checkpoint:
            raise ValidationError(
                code="CHECKPOINT_NOT_FOUND",
                message=f"Checkpoint {checkpoint_id} not found",
            )

        # Determine rollback action
        if checkpoint.created_state:
            # Operation was a create - delete what was created
            return self._rollback_create(checkpoint)
        elif checkpoint.previous_state:
            # Operation was update/delete - restore previous state
            return self._rollback_update(checkpoint)
        else:
            raise ValidationError(
                code="ROLLBACK_NOT_POSSIBLE",
                message="Checkpoint has no state to rollback to",
            )

    def _rollback_create(self, checkpoint: Checkpoint) -> dict[str, Any]:
        """Rollback a create operation by deleting created resource.

        Override in connector to implement actual deletion.
        """
        logger.info(f"Rollback create: would delete {checkpoint.created_state}")
        return {"action": "delete", "state": checkpoint.created_state}

    def _rollback_update(self, checkpoint: Checkpoint) -> dict[str, Any]:
        """Rollback an update/delete by restoring previous state.

        Override in connector to implement actual restoration.
        """
        logger.info(f"Rollback update: would restore {checkpoint.previous_state}")
        return {"action": "restore", "state": checkpoint.previous_state}

    def clear_checkpoint(self, checkpoint_id: str) -> None:
        """Clear a checkpoint (after successful operation or timeout)."""
        if checkpoint_id in self._checkpoints:
            del self._checkpoints[checkpoint_id]
            logger.debug(f"Cleared checkpoint {checkpoint_id}")
