"""Skill export functionality for creating .zip packages."""

import io
import json
import logging
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from nexus.core.exceptions import ValidationError
from nexus.skills.models import Skill, SkillExportManifest
from nexus.skills.registry import SkillNotFoundError, SkillRegistry

logger = logging.getLogger(__name__)


class SkillExportError(ValidationError):
    """Raised when skill export fails."""

    pass


class SkillExporter:
    """Export skills to .zip packages.

    Supports multiple export formats:
    - generic: Vendor-neutral format
    - claude: Anthropic Claude format (8MB limit)
    - openai: OpenAI format (future)

    Example:
        >>> exporter = SkillExporter(registry)
        >>> await exporter.export_skill(
        ...     "analyze-code",
        ...     output_path="analyze-code.zip",
        ...     format="claude"
        ... )
    """

    # Format size limits (in bytes)
    SIZE_LIMITS = {
        "claude": 8 * 1024 * 1024,  # 8MB for Claude
        "generic": None,  # No limit for generic format
    }

    def __init__(self, registry: SkillRegistry):
        """Initialize skill exporter.

        Args:
            registry: SkillRegistry instance
        """
        self._registry = registry

    async def export_skill(
        self,
        name: str,
        output_path: str | Path | None = None,
        format: str = "generic",
        include_dependencies: bool = True,
    ) -> bytes | None:
        """Export a skill to .zip format.

        Args:
            name: Skill name to export
            output_path: Optional path to write .zip file (if None, return bytes)
            format: Export format (generic, claude, openai)
            include_dependencies: If True, include all dependencies in export

        Returns:
            Zip file bytes if output_path is None, otherwise None

        Raises:
            SkillNotFoundError: If skill not found
            SkillExportError: If export fails or size limit exceeded

        Example:
            >>> # Export to file
            >>> await exporter.export_skill("analyze-code", "output.zip", format="claude")
            >>> # Get zip bytes
            >>> zip_bytes = await exporter.export_skill("analyze-code", format="generic")
        """
        # Validate format
        if format not in self.SIZE_LIMITS:
            raise SkillExportError(
                f"Unsupported export format: {format}. Supported: {list(self.SIZE_LIMITS.keys())}"
            )

        # Load skill
        try:
            skill = await self._registry.get_skill(name, load_dependencies=False)
        except SkillNotFoundError as e:
            raise SkillExportError(f"Skill not found: {name}") from e

        # Resolve dependencies if requested
        skills_to_export = [skill]
        if include_dependencies:
            dep_names = await self._registry.resolve_dependencies(name)
            # Remove the main skill (it's already first in the list)
            dep_names = [n for n in dep_names if n != name]

            for dep_name in dep_names:
                dep_skill = await self._registry.get_skill(dep_name)
                skills_to_export.append(dep_skill)

        # Create .zip package
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Track files and total size
            files_added = []
            total_size = 0

            # Add skills
            for skill_obj in skills_to_export:
                # Write SKILL.md file
                skill_filename = f"{skill_obj.metadata.name}/SKILL.md"
                skill_content = self._reconstruct_skill_md(skill_obj)

                zf.writestr(skill_filename, skill_content)
                files_added.append(skill_filename)
                total_size += len(skill_content.encode("utf-8"))

                logger.debug(f"Added {skill_filename} to export ({len(skill_content)} bytes)")

            # Create manifest
            manifest = SkillExportManifest(
                name=skill.metadata.name,
                version=skill.metadata.version,
                description=skill.metadata.description,
                format=format,
                exported_at=datetime.now(UTC),
                files=files_added,
                total_size_bytes=total_size,
            )

            # Validate manifest (checks size limits)
            try:
                manifest.validate()
            except ValidationError as e:
                raise SkillExportError(str(e)) from e

            # Add manifest.json
            manifest_json = json.dumps(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "description": manifest.description,
                    "format": manifest.format,
                    "exported_at": (
                        manifest.exported_at.isoformat() if manifest.exported_at else None
                    ),
                    "files": manifest.files,
                    "total_size_bytes": manifest.total_size_bytes,
                },
                indent=2,
            )
            zf.writestr("manifest.json", manifest_json)

            logger.info(
                f"Exported skill '{name}' ({total_size} bytes, "
                f"{len(files_added)} files, format={format})"
            )

        # Get zip bytes
        zip_bytes = zip_buffer.getvalue()

        # Write to file if output_path provided
        if output_path:
            output_path = Path(output_path)
            output_path.write_bytes(zip_bytes)
            logger.info(f"Wrote skill package to {output_path}")
            return None
        else:
            return zip_bytes

    def _reconstruct_skill_md(self, skill: Skill) -> str:
        """Reconstruct SKILL.md content from Skill object.

        Args:
            skill: Skill object

        Returns:
            Complete SKILL.md content (frontmatter + content)
        """
        # Build frontmatter dict
        from typing import Any

        frontmatter: dict[str, Any] = {
            "name": skill.metadata.name,
            "description": skill.metadata.description,
        }

        if skill.metadata.version:
            frontmatter["version"] = skill.metadata.version

        if skill.metadata.author:
            frontmatter["author"] = skill.metadata.author

        if skill.metadata.requires:
            frontmatter["requires"] = skill.metadata.requires

        if skill.metadata.created_at:
            frontmatter["created_at"] = skill.metadata.created_at.isoformat()

        if skill.metadata.modified_at:
            frontmatter["modified_at"] = skill.metadata.modified_at.isoformat()

        # Add additional metadata
        frontmatter.update(skill.metadata.metadata)

        # Convert to YAML
        import yaml

        frontmatter_yaml = yaml.dump(frontmatter, default_flow_style=False, sort_keys=False)

        # Reconstruct SKILL.md
        return f"---\n{frontmatter_yaml}---\n\n{skill.content}"

    async def validate_export(
        self,
        name: str,
        format: str = "generic",
        include_dependencies: bool = True,
    ) -> tuple[bool, str, int]:
        """Validate that a skill can be exported without actually creating the package.

        Args:
            name: Skill name
            format: Export format
            include_dependencies: If True, include dependencies in size calculation

        Returns:
            Tuple of (is_valid, message, total_size_bytes)

        Example:
            >>> valid, msg, size = await exporter.validate_export("analyze-code", "claude")
            >>> if not valid:
            ...     print(f"Cannot export: {msg}")
        """
        try:
            # Load skill
            skill = await self._registry.get_skill(name, load_dependencies=False)

            # Calculate size
            total_size = self._calculate_skill_size(skill)

            # Include dependencies if requested
            if include_dependencies:
                dep_names = await self._registry.resolve_dependencies(name)
                dep_names = [n for n in dep_names if n != name]

                for dep_name in dep_names:
                    dep_skill = await self._registry.get_skill(dep_name)
                    total_size += self._calculate_skill_size(dep_skill)

            # Check size limit
            size_limit = self.SIZE_LIMITS.get(format)
            if size_limit and total_size > size_limit:
                return (
                    False,
                    f"Export size {total_size / (1024 * 1024):.2f}MB exceeds "
                    f"{format} limit of {size_limit / (1024 * 1024):.2f}MB",
                    total_size,
                )

            return True, "Export is valid", total_size

        except Exception as e:
            return False, f"Validation failed: {e}", 0

    def _calculate_skill_size(self, skill: Skill) -> int:
        """Calculate the size of a skill in bytes.

        Args:
            skill: Skill object

        Returns:
            Size in bytes
        """
        content = self._reconstruct_skill_md(skill)
        return len(content.encode("utf-8"))

    async def import_skill(
        self,
        zip_path: str | Path | BinaryIO,
        tier: str = "agent",
        output_dir: str | Path | None = None,
    ) -> list[str]:
        """Import skills from a .zip package.

        Args:
            zip_path: Path to .zip file or file-like object
            tier: Tier to import skills to (agent, tenant, system)
            output_dir: Optional output directory (defaults to tier path)

        Returns:
            List of imported skill names

        Raises:
            SkillExportError: If import fails

        Example:
            >>> imported = await exporter.import_skill("analyze-code.zip", tier="agent")
            >>> print(f"Imported: {imported}")
        """
        # Determine output directory
        if output_dir is None:
            from nexus.skills.registry import SkillRegistry

            tier_path = SkillRegistry.TIER_PATHS.get(tier)
            if not tier_path:
                raise SkillExportError(f"Unknown tier: {tier}")
            output_dir = Path(tier_path)
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)

        # Open zip file
        if isinstance(zip_path, str | Path):
            zip_file = zipfile.ZipFile(zip_path, "r")
        else:
            zip_file = zipfile.ZipFile(zip_path, "r")

        imported_skills = []

        try:
            # Extract all files
            with zip_file:
                # Read manifest if present
                if "manifest.json" in zip_file.namelist():
                    manifest_content = zip_file.read("manifest.json").decode("utf-8")
                    manifest_data = json.loads(manifest_content)
                    logger.info(f"Importing skill package: {manifest_data.get('name')}")

                # Extract SKILL.md files
                for name in zip_file.namelist():
                    if name.endswith("SKILL.md"):
                        # Extract to output directory
                        target_path = output_dir / name
                        target_path.parent.mkdir(parents=True, exist_ok=True)

                        content = zip_file.read(name)
                        target_path.write_bytes(content)

                        # Extract skill name
                        skill_name = Path(name).parent.name
                        imported_skills.append(skill_name)

                        logger.info(f"Imported skill '{skill_name}' to {target_path}")

        except Exception as e:
            raise SkillExportError(f"Failed to import skill package: {e}") from e

        return imported_skills
