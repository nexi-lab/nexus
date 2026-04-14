"""Readme documentation generator — extracted from ReadmeDocMixin.

Converts connector metadata (Pydantic schemas, operation traits, error
registries) into README.md markdown and writes readme directories.
"""

import logging
import posixpath
from typing import Any

from pydantic import BaseModel

from nexus.backends.connectors.base import ConfirmLevel, ErrorDef, OpTraits

logger = logging.getLogger(__name__)


class ReadmeDocGenerator:
    """Generate README.md documentation from connector metadata.

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
    readme_dir:
        Directory name for readme docs (default: ``".readme"``).
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
        readme_dir: str = ".readme",
        nested_examples: dict[str, list[str]] | None = None,
        field_examples: dict[str, str] | None = None,
        write_paths: dict[str, str] | None = None,
    ) -> None:
        self._skill_name = skill_name
        self._schemas = schemas
        self._operation_traits = operation_traits
        self._error_registry = error_registry
        self._examples = examples
        self._readme_dir = readme_dir
        self._nested_examples = nested_examples or {}
        self._field_examples = field_examples or {}
        # operation_name -> write path (e.g., "send_email" -> "SENT/_new.yaml")
        self._write_paths = write_paths or {}
        # Optional directory structure description (set by connector)
        self._directory_structure: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_readme(self, mount_path: str) -> str:
        """Auto-generate README.md from connector metadata.

        Args:
            mount_path: The mount path for this connector.

        Returns:
            Generated README.md content as string.
        """
        lines = [
            f"# {self._format_display_name()} Connector",
            "",
            "## Mount Path",
            f"`{mount_path}`",
            "",
        ]

        # Directory structure (if provided)
        if self._directory_structure:
            lines.extend(
                ["## Directory Structure", "", "```", self._directory_structure, "```", ""]
            )

        # Read patterns + write operations (Issue #3148)
        lines.extend(self._generate_read_patterns_section(mount_path))

        if self._operation_traits:
            lines.extend(self._generate_required_format_section())

        if self._error_registry:
            lines.extend(self._generate_errors_section())

        return "\n".join(lines)

    def get_readme_path(self, mount_path: str) -> str:
        """Get the full path to the .readme directory."""
        return posixpath.join(mount_path.rstrip("/"), self._readme_dir)

    async def write_readme(self, mount_path: str, filesystem: Any = None) -> dict[str, Any]:
        """Generate and write .readme/ directory to the filesystem.

        Creates:
            <mount_path>/.readme/
                README.md           # Main documentation
                schemas/           # Individual schema YAML files (Issue #3148)
                    <operation>.yaml
                examples/          # Example YAML files
                    <example>.yaml

        Args:
            mount_path: The mount path for this connector.
            filesystem: NexusFS instance to write to (optional).

        Returns:
            Dict of written paths: {"readme_md": path, "schemas": [...], "examples": [...]}.
        """
        result: dict[str, Any] = {"readme_md": None, "schemas": [], "examples": []}

        if not self._skill_name:
            logger.warning("Cannot write readme docs: skill_name not configured")
            return result

        readme_dir = self.get_readme_path(mount_path)

        if filesystem is None:
            logger.debug("No filesystem provided for %s", self._skill_name)
            return result

        try:
            filesystem.mkdir(readme_dir, parents=True, exist_ok=True)

            # Write README.md
            readme_md_path = posixpath.join(readme_dir, "README.md")
            content = self.generate_readme(mount_path)
            filesystem.write(readme_md_path, content.encode("utf-8"))
            result["readme_md"] = readme_md_path
            logger.info("Generated README.md at %s", readme_md_path)

            # Write example files
            if self._examples:
                examples_dir = posixpath.join(readme_dir, "examples")
                filesystem.mkdir(examples_dir, parents=True, exist_ok=True)

                for filename, file_content in self._examples.items():
                    example_path = posixpath.join(examples_dir, filename)
                    filesystem.write(example_path, file_content.encode("utf-8"))
                    result["examples"].append(example_path)
                    logger.debug("Generated example at %s", example_path)

            # Write individual schema files (Issue #3148, Decision #7B)
            if self._schemas:
                schemas_dir = posixpath.join(readme_dir, "schemas")
                filesystem.mkdir(schemas_dir, parents=True, exist_ok=True)

                for op_name, schema in self._schemas.items():
                    schema_content = self.generate_schema_yaml(op_name, schema)
                    schema_path = posixpath.join(schemas_dir, f"{op_name}.yaml")
                    filesystem.write(schema_path, schema_content.encode("utf-8"))
                    result["schemas"].append(schema_path)
                    logger.debug("Generated schema at %s", schema_path)

            return result

        except Exception as e:
            logger.warning("Failed to write readme docs to %s: %s", readme_dir, e)
            return result

    def _generate_read_patterns_section(self, mount_path: str) -> list[str]:
        """Generate Read Patterns section showing how to list, cat, grep content.

        Provides agents with L0-L1 discovery: how to explore connector content
        before attempting write operations. Issue #3148.
        """
        mp = mount_path.rstrip("/")
        lines = [
            "## Read Patterns",
            "",
            "### List content",
            "```bash",
            f"nexus ls {mp}/",
            "```",
            "",
            "### Read a file",
            "```bash",
            f"nexus cat {mp}/<path>",
            "```",
            "",
            "### Search content",
            "```bash",
            f'nexus grep "keyword" {mp}/',
            "```",
            "",
        ]

        # Add write operations with exact paths and inline schemas
        if self._schemas:
            lines.extend(["## Operations", ""])

            for op_name, schema in self._schemas.items():
                traits = self._operation_traits.get(op_name, OpTraits())
                display = op_name.replace("_", " ").title()
                write_path = self._write_paths.get(op_name, "_new.yaml")

                lines.append(f"### {display}")
                lines.append("")
                lines.append(f"Write to `{mp}/{write_path}`:")
                lines.append(f"- Reversibility: **{traits.reversibility.value}**")
                lines.append(f"- Confirm: **{traits.confirm.value}**")

                if traits.confirm == ConfirmLevel.USER:
                    lines.append("- **⚠ IRREVERSIBLE** — requires `user_confirmed: true`")

                lines.append("")
                lines.append("```yaml")

                if traits.confirm >= ConfirmLevel.INTENT:
                    lines.append("# agent_intent: <why you are doing this — min 10 chars>")
                if traits.confirm >= ConfirmLevel.EXPLICIT:
                    lines.append("# confirm: true")
                if traits.confirm == ConfirmLevel.USER:
                    lines.append("# user_confirmed: true  # ask user first")

                # Inline schema fields
                for field_name, field_info in schema.model_fields.items():
                    if field_name in ("agent_intent", "confirm", "user_confirmed"):
                        continue
                    required = field_info.is_required()
                    req_tag = "REQUIRED" if required else "optional"
                    desc = field_info.description or ""
                    example = self._get_field_example(
                        field_name, field_info, field_info.annotation, required
                    )
                    lines.append(
                        f"{field_name}: {example}  # {req_tag}{' — ' + desc if desc else ''}"
                    )

                lines.append("```")
                lines.append("")

                for warning in traits.warnings:
                    lines.append(f"> **Warning:** {warning}")
                    lines.append("")

            lines.append("")

        # Add schema discovery
        lines.extend(
            [
                "### Schema discovery",
                "```bash",
                f"nexus mounts skills {mp}",
                f"nexus mounts schema {mp} <operation>",
                "```",
                "",
            ]
        )

        return lines

    def generate_schema_yaml(self, op_name: str, schema: type[BaseModel]) -> str:
        """Generate an annotated YAML schema file for a single operation.

        Each field includes type, required/optional, constraints, and description
        from Pydantic field metadata. This is the L2 discovery layer that agents
        use to construct valid writes.

        Args:
            op_name: Operation name (e.g., "send_email").
            schema: Pydantic model class.

        Returns:
            Annotated YAML content as string.
        """
        traits = self._operation_traits.get(op_name, OpTraits())
        lines = [
            f"# Schema: {op_name}",
            f"# Connector: {self._format_display_name()}",
            f"# Reversibility: {traits.reversibility.value}",
            f"# Confirm level: {traits.confirm.value}",
            "#",
        ]

        if traits.confirm >= ConfirmLevel.INTENT:
            lines.append("# agent_intent: <required, min 10 chars — why you are doing this>")
        if traits.confirm >= ConfirmLevel.EXPLICIT:
            lines.append("# confirm: true  # REQUIRED")

        lines.append("")

        for field_name, field_info in schema.model_fields.items():
            if field_name in ("agent_intent", "confirm", "user_confirmed"):
                continue

            annotation = field_info.annotation
            required = field_info.is_required()
            description = field_info.description or ""
            req_label = "required" if required else "optional"

            # Type name
            type_name = self._get_type_name(annotation)

            # Constraints from metadata
            constraints = self._get_field_constraints(field_info)
            constraint_str = f", {constraints}" if constraints else ""

            comment = f"# {req_label}, {type_name}{constraint_str}"
            if description:
                comment += f" — {description}"

            lines.append(comment)

            example = self._get_field_example(field_name, field_info, annotation, required)
            lines.append(f"{field_name}: {example}")
            lines.append("")

        return "\n".join(lines)

    @staticmethod
    def _get_type_name(annotation: Any) -> str:
        """Get human-readable type name from annotation."""
        if annotation is None:
            return "any"
        origin = getattr(annotation, "__origin__", None)
        if origin is list:
            args = getattr(annotation, "__args__", ())
            inner = args[0].__name__ if args else "any"
            return f"list[{inner}]"
        if origin is dict:
            return "dict"
        if hasattr(annotation, "__name__"):
            return str(annotation.__name__)
        return str(annotation)

    @staticmethod
    def _get_field_constraints(field_info: Any) -> str:
        """Extract constraint string from Pydantic field metadata."""
        parts = []
        for meta in field_info.metadata or []:
            if hasattr(meta, "min_length") and meta.min_length is not None:
                parts.append(f"min_length={meta.min_length}")
            if hasattr(meta, "max_length") and meta.max_length is not None:
                parts.append(f"max_length={meta.max_length}")
            if hasattr(meta, "ge") and meta.ge is not None:
                parts.append(f"min={meta.ge}")
            if hasattr(meta, "le") and meta.le is not None:
                parts.append(f"max={meta.le}")
            if hasattr(meta, "pattern") and meta.pattern is not None:
                parts.append(f"pattern={meta.pattern}")
        return ", ".join(parts)

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
