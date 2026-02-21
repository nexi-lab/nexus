"""Base mixins for connector validation framework.

This module provides opt-in mixins that connectors can use to add:
- SKILL.md documentation (auto-generated)
- Pydantic schema validation
- Operation traits (reversibility, confirmation levels)
- Checkpoint/rollback support

Each connector configures these mixins via class attributes.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from nexus.core.exceptions import ValidationError as CoreValidationError

if TYPE_CHECKING:
    from nexus.backends.connectors.error_formatter import SkillErrorFormatter
    from nexus.backends.connectors.schema_generator import SkillDocGenerator
    from nexus.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


# =============================================================================
# Enums & Data Classes
# =============================================================================


class Reversibility(StrEnum):
    """How reversible an operation is."""

    FULL = "full"  # Can undo completely (e.g., delete created event)
    PARTIAL = "partial"  # Can undo with limitations (e.g., restore from trash)
    NONE = "none"  # Cannot undo (e.g., send email)


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
        return {"none": 0, "intent": 1, "explicit": 2, "user": 3}[self.value]

    def __ge__(self, other: object) -> bool:
        """Compare levels by strictness."""
        if isinstance(other, ConfirmLevel):
            return self.level >= other.level
        return NotImplemented

    def __gt__(self, other: object) -> bool:
        """Compare levels by strictness."""
        if isinstance(other, ConfirmLevel):
            return self.level > other.level
        return NotImplemented

    def __le__(self, other: object) -> bool:
        """Compare levels by strictness."""
        if isinstance(other, ConfirmLevel):
            return self.level <= other.level
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        """Compare levels by strictness."""
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
    that include fix examples and SKILL.md references.

    Example:
        >>> ERROR_REGISTRY = {
        ...     "MISSING_AGENT_INTENT": ErrorDef(
        ...         message="Operations require agent_intent",
        ...         skill_section="required-format",
        ...         fix_example="# agent_intent: User requested meeting",
        ...     ),
        ... }
    """

    message: str
    skill_section: str  # SKILL.md section anchor
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
        skill_path: str | None = None,
        skill_section: str | None = None,
        fix_example: str | None = None,
        field_errors: dict[str, str] | None = None,
    ):
        self.code = code
        self.message = message
        self.skill_path = skill_path
        self.skill_section = skill_section
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

        if self.skill_path:
            ref = self.skill_path
            if self.skill_section:
                ref += f"#{self.skill_section}"
            lines.append(f"\nSee: {ref}")

        if self.fix_example:
            lines.append(f"\nFix:\n```yaml\n{self.fix_example}\n```")

        return "\n".join(lines)


# =============================================================================
# SkillDocMixin - SKILL.md Integration
# =============================================================================


class SkillDocMixin:
    """Mixin for SKILL.md integration with auto-generation.

    Connectors configure:
        SKILL_NAME: str - Skill identifier (e.g., "gcalendar")
        SKILL_DIR: str - Directory name for skill docs (default: ".skill")

    Features:
        - Auto-generates .skill/ directory with SKILL.md and examples
        - Integrates with SkillRegistry for discovery
        - Formats errors with skill references

    Delegates heavy lifting to ``SkillDocGenerator`` and ``SkillErrorFormatter``.
    """

    SKILL_NAME: str = ""
    SKILL_DIR: str = ".skill"  # Directory at mount path

    # Subclasses provide these (used for auto-generation)
    SCHEMAS: dict[str, type[BaseModel]] = {}
    OPERATION_TRAITS: dict[str, OpTraits] = {}
    ERROR_REGISTRY: dict[str, ErrorDef] = {}
    EXAMPLES: dict[str, str] = {}  # Example files: {"create_meeting.yaml": "content..."}

    _skill_registry: SkillRegistry | None = None
    _mount_path: str | None = None  # Set during mount

    @property
    def skill_md_path(self) -> str:
        """Get path to SKILL.md (for error messages)."""
        if self._mount_path:
            return posixpath.join(self._mount_path.rstrip("/"), self.SKILL_DIR, "SKILL.md")
        return "/.skill/SKILL.md"  # Default fallback

    def set_skill_registry(self, registry: SkillRegistry) -> None:
        """Set the skill registry for this connector."""
        self._skill_registry = registry

    def set_mount_path(self, mount_path: str) -> None:
        """Set the mount path (called during mount)."""
        self._mount_path = mount_path

    def _get_doc_generator(self) -> SkillDocGenerator:
        """Lazy-create the SkillDocGenerator."""
        from nexus.backends.connectors.schema_generator import SkillDocGenerator

        return SkillDocGenerator(
            skill_name=self.SKILL_NAME,
            schemas=self.SCHEMAS,
            operation_traits=self.OPERATION_TRAITS,
            error_registry=self.ERROR_REGISTRY,
            examples=self.EXAMPLES,
            skill_dir=self.SKILL_DIR,
        )

    def _get_error_formatter(self) -> SkillErrorFormatter:
        """Lazy-create the SkillErrorFormatter."""
        from nexus.backends.connectors.error_formatter import SkillErrorFormatter

        return SkillErrorFormatter(
            skill_name=self.SKILL_NAME,
            mount_path=self._mount_path or "",
        )

    def generate_skill_doc(self, mount_path: str) -> str:
        """Auto-generate SKILL.md from connector metadata."""
        return self._get_doc_generator().generate_skill_doc(mount_path)

    def get_skill_path(self, mount_path: str) -> str:
        """Get the full path to the .skill directory."""
        return self._get_doc_generator().get_skill_path(mount_path)

    def write_skill_docs(self, mount_path: str, filesystem: Any = None) -> dict[str, str]:
        """Generate and write .skill/ directory to the filesystem."""
        self._mount_path = mount_path
        return self._get_doc_generator().write_skill_docs(mount_path, filesystem)

    def _format_display_name(self) -> str:
        """Format SKILL_NAME as display name."""
        return self.SKILL_NAME.replace("_", " ").replace("-", " ").title()

    def _generate_operations_section(self) -> list[str]:
        """Generate Operations section from SCHEMAS."""
        return self._get_doc_generator()._generate_operations_section()

    def _schema_to_yaml_lines(self, schema: type[BaseModel]) -> list[str]:
        """Convert Pydantic schema to YAML example lines."""
        return self._get_doc_generator()._schema_to_yaml_lines(schema)

    def _is_nested_model(self, annotation: Any) -> bool:
        """Check if annotation is a nested Pydantic model."""
        return self._get_doc_generator()._is_nested_model(annotation)

    def _get_nested_example(self, field_name: str, _annotation: Any, required: bool) -> list[str]:
        """Get example lines for nested model."""
        return self._get_doc_generator()._get_nested_example(field_name, _annotation, required)

    def _get_field_example(
        self, field_name: str, _field_info: Any, annotation: Any, required: bool
    ) -> str:
        """Get example value for a field."""
        return self._get_doc_generator()._get_field_example(
            field_name, _field_info, annotation, required
        )

    def _format_type_hint(self, annotation: Any) -> str:
        """Format type annotation as readable string."""
        return self._get_doc_generator()._format_type_hint(annotation)

    def _generate_required_format_section(self) -> list[str]:
        """Generate Required Format section from OPERATION_TRAITS."""
        return self._get_doc_generator()._generate_required_format_section()

    def _generate_errors_section(self) -> list[str]:
        """Generate Error Codes section from ERROR_REGISTRY."""
        return self._get_doc_generator()._generate_errors_section()

    def format_error_with_skill_ref(
        self,
        code: str,
        message: str,
        section: str | None = None,
        fix_example: str | None = None,
    ) -> ValidationError:
        """Create ValidationError with skill reference."""
        return self._get_error_formatter().format_error_with_skill_ref(
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
            return cast(BaseModel, data)

        try:
            return schema.model_validate(data)
        except PydanticValidationError as e:
            from nexus.backends.connectors.error_formatter import SkillErrorFormatter

            field_errors = {}
            for error in e.errors():
                loc = ".".join(str(x) for x in error["loc"])
                field_errors[loc] = error["msg"]

            skill_name = getattr(self, "SKILL_NAME", "")
            mount_path = getattr(self, "_mount_path", "") or ""
            formatter = SkillErrorFormatter(skill_name=skill_name, mount_path=mount_path)
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
        from nexus.backends.connectors.error_formatter import SkillErrorFormatter

        skill_name = getattr(self, "SKILL_NAME", "")
        mount_path = getattr(self, "_mount_path", "") or ""
        formatter = SkillErrorFormatter(skill_name=skill_name, mount_path=mount_path)
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

    def __init__(self) -> None:
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
