"""Skill documentation generator — extracted from SkillDocMixin.

Converts connector metadata (Pydantic schemas, operation traits, error
registries) into SKILL.md markdown and writes skill directories.
"""

import logging
import posixpath
from typing import Any

from pydantic import BaseModel

from nexus.backends.connectors.base import ConfirmLevel, ErrorDef, OpTraits

logger = logging.getLogger(__name__)


class SkillDocGenerator:
    """Generate SKILL.md documentation from connector metadata.

    Parameters
    ----------
    skill_name:
        Skill identifier (e.g., ``"gcalendar"``).
    schemas:
        Operation name → Pydantic model mapping.
    operation_traits:
        Operation name → OpTraits mapping.
    error_registry:
        Error code → ErrorDef mapping.
    examples:
        Example files: ``{"create_meeting.yaml": "content..."}``.
    skill_dir:
        Directory name for skill docs (default: ``".skill"``).
    nested_examples:
        Configurable nested-field examples (overrides defaults).
    """

    def __init__(
        self,
        skill_name: str,
        schemas: dict[str, type[BaseModel]],
        operation_traits: dict[str, OpTraits],
        error_registry: dict[str, ErrorDef],
        examples: dict[str, str],
        skill_dir: str = ".skill",
        nested_examples: dict[str, list[str]] | None = None,
        field_examples: dict[str, str] | None = None,
    ) -> None:
        self._skill_name = skill_name
        self._schemas = schemas
        self._operation_traits = operation_traits
        self._error_registry = error_registry
        self._examples = examples
        self._skill_dir = skill_dir
        self._nested_examples = nested_examples or {}
        self._field_examples = field_examples or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_skill_doc(self, mount_path: str) -> str:
        """Auto-generate SKILL.md from connector metadata.

        Args:
            mount_path: The mount path for this connector.

        Returns:
            Generated SKILL.md content as string.
        """
        lines = [
            f"# {self._format_display_name()} Connector",
            "",
            "## Mount Path",
            f"`{mount_path}`",
            "",
        ]

        if self._schemas:
            lines.extend(self._generate_operations_section())

        if self._operation_traits:
            lines.extend(self._generate_required_format_section())

        if self._error_registry:
            lines.extend(self._generate_errors_section())

        return "\n".join(lines)

    def get_skill_path(self, mount_path: str) -> str:
        """Get the full path to the .skill directory."""
        return posixpath.join(mount_path.rstrip("/"), self._skill_dir)

    def write_skill_docs(self, mount_path: str, filesystem: Any = None) -> dict[str, Any]:
        """Generate and write .skill/ directory to the filesystem.

        Creates:
            <mount_path>/.skill/
                SKILL.md           # Main documentation
                examples/          # Example YAML files
                    <example>.yaml

        Args:
            mount_path: The mount path for this connector.
            filesystem: NexusFS instance to write to (optional).

        Returns:
            Dict of written paths: {"skill_md": path, "examples": [paths...]}.
        """
        result: dict[str, Any] = {"skill_md": None, "examples": []}

        if not self._skill_name:
            logger.warning("Cannot write skill docs: skill_name not configured")
            return result

        skill_dir = self.get_skill_path(mount_path)

        if filesystem is None:
            logger.debug("No filesystem provided for %s", self._skill_name)
            return result

        try:
            filesystem.sys_mkdir(skill_dir, parents=True, exist_ok=True)

            skill_md_path = posixpath.join(skill_dir, "SKILL.md")
            content = self.generate_skill_doc(mount_path)
            filesystem.sys_write(skill_md_path, content.encode("utf-8"))
            result["skill_md"] = skill_md_path
            logger.info("Generated SKILL.md at %s", skill_md_path)

            if self._examples:
                examples_dir = posixpath.join(skill_dir, "examples")
                filesystem.sys_mkdir(examples_dir, parents=True, exist_ok=True)

                for filename, file_content in self._examples.items():
                    example_path = posixpath.join(examples_dir, filename)
                    filesystem.sys_write(example_path, file_content.encode("utf-8"))
                    result["examples"].append(example_path)
                    logger.debug("Generated example at %s", example_path)

            return result

        except Exception as e:
            logger.warning("Failed to write skill docs to %s: %s", skill_dir, e)
            return result

    # ------------------------------------------------------------------
    # Section generators
    # ------------------------------------------------------------------

    def _generate_operations_section(self) -> list[str]:
        """Generate Operations section from SCHEMAS."""
        lines = ["## Operations", ""]

        for op_name, schema in self._schemas.items():
            display_name = op_name.replace("_", " ").title()
            lines.append(f"### {display_name}")
            lines.append("")

            traits = self._operation_traits.get(op_name, OpTraits())

            lines.append("```yaml")

            if traits.confirm >= ConfirmLevel.INTENT:
                lines.append("# agent_intent: <reason for this operation>")

            if traits.confirm >= ConfirmLevel.EXPLICIT:
                lines.append("# confirm: true")

            lines.extend(self._schema_to_yaml_lines(schema))
            lines.append("```")
            lines.append("")

            for warning in traits.warnings:
                lines.append(f"> **Warning:** {warning}")
                lines.append("")

        return lines

    def _generate_required_format_section(self) -> list[str]:
        """Generate Required Format section from OPERATION_TRAITS."""
        lines = ["## Required Format", ""]

        intent_ops = []
        explicit_ops = []
        user_ops = []

        for op_name, traits in self._operation_traits.items():
            if traits.confirm == ConfirmLevel.USER:
                user_ops.append(op_name)
            elif traits.confirm == ConfirmLevel.EXPLICIT:
                explicit_ops.append(op_name)
            elif traits.confirm == ConfirmLevel.INTENT:
                intent_ops.append(op_name)

        if intent_ops or explicit_ops or user_ops:
            lines.append("All operations require `# agent_intent: <reason>` as the first line.")
            lines.append("")

        if explicit_ops:
            ops_str = ", ".join(f"`{op}`" for op in explicit_ops)
            lines.append(f"Operations requiring explicit confirmation ({ops_str}):")
            lines.append("- Add `# confirm: true` after agent_intent")
            lines.append("")

        if user_ops:
            ops_str = ", ".join(f"`{op}`" for op in user_ops)
            lines.append(f"Operations requiring user confirmation ({ops_str}):")
            lines.append("- Add `# user_confirmed: true` after getting explicit user approval")
            lines.append("- **These operations CANNOT be undone**")
            lines.append("")

        return lines

    def _generate_errors_section(self) -> list[str]:
        """Generate Error Codes section from ERROR_REGISTRY."""
        lines = ["## Error Codes", ""]

        for code, error_def in self._error_registry.items():
            lines.append(f"### {code}")
            lines.append(error_def.message)
            lines.append("")

            if error_def.fix_example:
                lines.append("**Fix:**")
                lines.append("```yaml")
                lines.append(error_def.fix_example)
                lines.append("```")
                lines.append("")

        return lines

    # ------------------------------------------------------------------
    # Schema → YAML helpers
    # ------------------------------------------------------------------

    def _schema_to_yaml_lines(self, schema: type[BaseModel]) -> list[str]:
        """Convert Pydantic schema to YAML example lines."""
        lines = []

        for field_name, field_info in schema.model_fields.items():
            if field_name in ("agent_intent", "confirm"):
                continue

            annotation = field_info.annotation
            required = field_info.is_required()
            default = field_info.default

            example = self._get_field_example(field_name, field_info, annotation, required)

            if self._is_nested_model(annotation):
                lines.append(f"{field_name}:")
                nested_lines = self._get_nested_example(field_name, annotation, required)
                lines.extend(f"  {line}" for line in nested_lines)
            elif default is not None and str(default) not in ("PydanticUndefined", "..."):
                if isinstance(default, bool):
                    lines.append(f"{field_name}: {str(default).lower()}")
                elif isinstance(default, list):
                    lines.append(f"{field_name}: []")
                else:
                    lines.append(f"{field_name}: {default}")
            else:
                lines.append(f"{field_name}: {example}")

        return lines

    def _is_nested_model(self, annotation: Any) -> bool:
        """Check if annotation is a nested Pydantic model."""
        try:
            import types

            if isinstance(annotation, types.UnionType):
                args = getattr(annotation, "__args__", ())
                return any(arg is not type(None) and hasattr(arg, "model_fields") for arg in args)

            origin = getattr(annotation, "__origin__", None)
            if origin is type(None) or str(origin) == "typing.Union":
                args = getattr(annotation, "__args__", ())
                for arg in args:
                    if arg is not type(None) and hasattr(arg, "model_fields"):
                        return True
            return hasattr(annotation, "model_fields")
        except Exception:
            return False

    def _get_nested_example(self, field_name: str, _annotation: Any, required: bool) -> list[str]:
        """Get example lines for nested model."""
        if field_name in self._nested_examples:
            return list(self._nested_examples[field_name])

        suffix = ", required" if required else ", optional"
        return [f"# <nested object{suffix}>"]

    def _get_field_example(
        self, field_name: str, _field_info: Any, annotation: Any, required: bool
    ) -> str:
        """Get example value for a field.

        Checks connector-provided ``field_examples`` first, then falls
        back to generic type-based placeholders.
        """
        if field_name in self._field_examples:
            return self._field_examples[field_name]

        type_hint = self._format_type_hint(annotation)
        suffix = ", required" if required else ", optional"

        if "list" in type_hint.lower():
            return "[]"
        elif "bool" in type_hint.lower():
            return "true"
        elif "int" in type_hint.lower():
            return "0"

        return f"<{type_hint}{suffix}>"

    def _format_type_hint(self, annotation: Any) -> str:
        """Format type annotation as readable string."""
        if annotation is None:
            return "any"

        type_name = getattr(annotation, "__name__", str(annotation))

        if "str" in type_name.lower():
            return "string"
        elif "int" in type_name.lower():
            return "integer"
        elif "bool" in type_name.lower():
            return "boolean"
        elif "list" in type_name.lower():
            return "list"
        elif "dict" in type_name.lower():
            return "object"

        return type_name

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_display_name(self) -> str:
        """Format skill_name as display name."""
        return self._skill_name.replace("_", " ").replace("-", " ").title()
