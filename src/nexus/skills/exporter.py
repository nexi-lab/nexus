"""Skill export functionality for creating .skill (ZIP) packages."""

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

from nexus.core.exceptions import ValidationError
from nexus.skills.models import Skill
from nexus.skills.registry import SkillNotFoundError, SkillRegistry

logger = logging.getLogger(__name__)


class SkillExportError(ValidationError):
    """Raised when skill export fails."""

    pass


class SkillExporter:
    """Export skills to .skill (ZIP) packages.

    Exports the entire skill directory as a ZIP file, preserving the folder structure.
    The exported file can be imported back to restore the complete skill.

    Example:
        >>> exporter = SkillExporter(registry)
        >>> await exporter.export_skill(
        ...     "analyze-code",
        ...     output_path="analyze-code.skill"
        ... )
    """

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
        include_dependencies: bool = True,
        format: str = "generic",
        context: Any = None,
    ) -> bytes | None:
        """Export a skill to .skill (zip) format.

        Args:
            name: Skill name to export
            output_path: Optional path to write .skill file (if None, return bytes)
            include_dependencies: If True, include all dependencies in export
            context: Operation context for permission checks and file access

        Returns:
            Zip file bytes if output_path is None, otherwise None

        Raises:
            SkillNotFoundError: If skill not found
            SkillExportError: If export fails

        Example:
            >>> # Export to file
            >>> await exporter.export_skill("analyze-code", "analyze-code.skill")
            >>> # Get zip bytes
            >>> zip_bytes = await exporter.export_skill("analyze-code")
        """
        # Validate format
        supported_formats = {"generic", "claude"}
        if format not in supported_formats:
            raise SkillExportError(f"Unsupported export format: {format}")

        # Load skill
        try:
            skill = await self._registry.get_skill(name, context=context, load_dependencies=False)
        except SkillNotFoundError as e:
            raise SkillExportError(f"Skill not found: {name}") from e

        # Resolve dependencies if requested
        skills_to_export = [skill]
        if include_dependencies:
            dep_names = await self._registry.resolve_dependencies(name)
            # Remove the main skill (it's already first in the list)
            dep_names = [n for n in dep_names if n != name]

            for dep_name in dep_names:
                dep_skill = await self._registry.get_skill(dep_name, context=context)
                skills_to_export.append(dep_skill)

        # Create .zip package
        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Track files and total size
            files_added = []
            total_size = 0
            manifest_files: list[str] = []

            # Add skills (export entire skill directory)
            for skill_obj in skills_to_export:
                skill_files, skill_size = await self._add_skill_directory_to_zip(
                    skill_obj, zf, context
                )
                files_added.extend(skill_files)
                total_size += skill_size
                manifest_files.extend(skill_files)

            # Basic validation
            if not files_added:
                raise SkillExportError(f"No files found to export for skill '{name}'")

            # Add manifest.json
            manifest = {
                "name": skill.metadata.name,
                "version": skill.metadata.version or "unknown",
                "format": format,
                "files": manifest_files,
                "total_size_bytes": total_size,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            logger.info(f"Exported skill '{name}' ({total_size} bytes, {len(files_added)} files)")

        # Enforce format-specific limits
        if format == "claude" and total_size > 8 * 1024 * 1024:
            raise SkillExportError("Skill package exceeds Claude format 8MB limit")

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

    async def _add_skill_directory_to_zip(
        self,
        skill: Skill,
        zip_file: zipfile.ZipFile,
        context: Any = None,
    ) -> tuple[list[str], int]:
        """Add entire skill directory to zip file.

        This is the reverse of import: it zips the entire skill directory
        (including all files like scripts/, references/, assets/, etc.)
        to create a .zip/.skill package.

        Args:
            skill: Skill object to export
            zip_file: ZipFile object to write to
            context: Operation context for permission checks and file access

        Returns:
            Tuple of (list of file paths added, total size in bytes)
        """
        if not skill.metadata.file_path:
            raise SkillExportError(
                f"Cannot export skill '{skill.metadata.name}': file_path not set"
            )

        # Get skill directory path (parent of SKILL.md)
        # Normalize to remove trailing slashes for consistent path handling
        skill_dir_path = str(Path(skill.metadata.file_path).parent).rstrip("/")
        skill_name = skill.metadata.name

        logger.debug(f"Exporting skill '{skill_name}' from directory: {skill_dir_path}")

        # Get filesystem from registry
        if not self._registry._filesystem:
            raise SkillExportError(f"Cannot export skill '{skill_name}': filesystem not available")

        filesystem = self._registry._filesystem

        # List all files in skill directory recursively
        try:
            all_files = filesystem.list(path=skill_dir_path, recursive=True, context=context)
        except Exception as e:
            raise SkillExportError(
                f"Failed to list files in skill directory '{skill_dir_path}': {e}"
            ) from e

        # Separate files from directories
        # Note: filesystem.list() may return directories with OR without trailing "/"
        # We need to check each entry to determine if it's a directory
        file_paths = []
        directory_paths = []

        for entry in all_files:
            # Type guard: only process string entries
            if not isinstance(entry, str):
                continue

            # Skip virtual parsed views
            if entry.endswith("_parsed.md") or entry.endswith("_parsed.txt"):
                continue

            # Check if entry is a directory
            # Method 1: Check if path ends with "/" (when list() returns it correctly)
            if entry.endswith("/"):
                # Strip trailing slash for consistent handling
                directory_paths.append(entry.rstrip("/"))
            # Method 2: Use filesystem.is_directory() for entries without trailing "/"
            elif filesystem.is_directory(entry, context=context):
                directory_paths.append(entry)
            else:
                # It's a file
                file_paths.append(entry)

        logger.debug(
            f"Found {len(all_files)} total entries, {len(file_paths)} files, {len(directory_paths)} directories"
        )
        if file_paths:
            logger.debug(f"Sample file paths: {file_paths[:5]}")
        if directory_paths:
            logger.debug(f"Sample directory paths: {directory_paths[:5]}")

        # Track which directories have files (so we know which are empty)
        directories_with_files = set()
        for file_path in file_paths:
            try:
                file_path_obj = Path(file_path)
                skill_dir_path_obj = Path(skill_dir_path)
                relative_path = file_path_obj.relative_to(skill_dir_path_obj)
                # Add all parent directories to the set
                for parent in relative_path.parents:
                    if str(parent) != ".":
                        directories_with_files.add(str(parent).replace("\\", "/"))
            except ValueError:
                pass

        files_added = []
        total_size = 0

        # Find empty directories (directories that have no files)
        # We only need to explicitly add empty directories - zipfile will automatically
        # create parent directories when we add files
        empty_directories = set()

        # Get all directories from directory_paths
        for dir_path in directory_paths:
            try:
                dir_path_obj = Path(dir_path)
                skill_dir_path_obj = Path(skill_dir_path)
                relative_dir_path = dir_path_obj.relative_to(skill_dir_path_obj)
                relative_dir_str = str(relative_dir_path).replace("\\", "/")
                empty_directories.add(relative_dir_str)
            except ValueError:
                continue

        # Remove directories that have files (they'll be created automatically)
        directories_with_files = set()
        for file_path in file_paths:
            try:
                file_path_obj = Path(file_path.rstrip("/"))
                skill_dir_path_obj = Path(skill_dir_path)
                relative_path = file_path_obj.relative_to(skill_dir_path_obj)
                # Add all parent directories to the set
                for parent in relative_path.parents:
                    if str(parent) != ".":
                        directories_with_files.add(str(parent).replace("\\", "/"))
            except ValueError:
                pass

        # Only keep truly empty directories
        empty_directories = empty_directories - directories_with_files

        # Add empty directories explicitly using ZipInfo
        directories_added = set()
        for relative_dir_str in sorted(empty_directories):
            zip_dir_path = f"{skill_name}/{relative_dir_str}/"
            if zip_dir_path not in directories_added:
                # Use ZipInfo to explicitly mark as directory
                zip_info = zipfile.ZipInfo(zip_dir_path)
                zip_info.external_attr = 0o40755 << 16  # Unix directory permissions
                zip_file.writestr(zip_info, "")
                directories_added.add(zip_dir_path)
                logger.debug(f"Added empty directory {zip_dir_path}")

        # Add each file to zip (zipfile will automatically create parent directories)
        logger.info(f"Found {len(file_paths)} files to add to zip")
        for file_path in file_paths:
            try:
                # Read file content
                raw_content = filesystem.read(file_path, context=context)
                assert isinstance(raw_content, bytes), "Expected bytes from read()"

                # Calculate relative path from skill directory using Path objects
                # This ensures proper path handling across platforms
                # e.g., /tenant:default/user:admin/skill/my-skill/scripts/helper.py
                # skill_dir_path: /tenant:default/user:admin/skill/my-skill
                # becomes: scripts/helper.py
                try:
                    # Normalize file path (remove trailing slash if present)
                    normalized_file_path = file_path.rstrip("/")

                    # Use Path objects to properly calculate relative path
                    file_path_obj = Path(normalized_file_path)
                    skill_dir_path_obj = Path(skill_dir_path)

                    # Get relative path (this handles path separators correctly)
                    relative_path = file_path_obj.relative_to(skill_dir_path_obj)
                    # Convert to string with forward slashes (required by zipfile)
                    relative_path_str = str(relative_path).replace("\\", "/")
                except ValueError as ve:
                    # Fallback if paths don't share a common root
                    logger.warning(f"Could not calculate relative path for '{file_path}': {ve}")
                    # Extract just the filename
                    relative_path_str = Path(file_path).name

                # Zip path should always start with skill name
                # Use forward slashes (zipfile standard)
                zip_path = f"{skill_name}/{relative_path_str}"

                logger.debug(
                    f"File: {file_path} -> relative: {relative_path_str} -> zip_path: {zip_path}"
                )

                # Add to zip (zipfile automatically creates all parent directory entries)
                zip_file.writestr(zip_path, raw_content)
                files_added.append(zip_path)
                total_size += len(raw_content)

                logger.debug(f"Added {zip_path} to export ({len(raw_content)} bytes)")

            except Exception as e:
                logger.error(f"Failed to add file '{file_path}' to export: {e}", exc_info=True)
                # Continue with other files

        if not files_added:
            raise SkillExportError(
                f"No files found in skill directory '{skill_dir_path}' for '{skill_name}'"
            )

        logger.info(
            f"Exported skill directory '{skill_name}': {len(directories_added)} directories, "
            f"{len(files_added)} files, {total_size} bytes"
        )

        if files_added:
            logger.debug(f"Sample files added: {files_added[:5]}")

        return files_added, total_size

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
        include_dependencies: bool = True,
        format: str = "generic",
        context: Any = None,
    ) -> tuple[bool, str, int]:
        """Validate that a skill can be exported without actually creating the package.

        Args:
            name: Skill name
            include_dependencies: If True, include dependencies in size calculation
            context: Operation context for permission checks

        Returns:
            Tuple of (is_valid, message, total_size_bytes)

        Example:
            >>> valid, msg, size = await exporter.validate_export("analyze-code")
            >>> if not valid:
            ...     print(f"Cannot export: {msg}")
        """
        try:
            supported_formats = {"generic", "claude"}
            if format not in supported_formats:
                return False, f"Unsupported export format: {format}", 0

            # Load skill
            skill = await self._registry.get_skill(name, context=context, load_dependencies=False)

            # Calculate size
            total_size = await self._calculate_skill_size(skill, context)

            # Include dependencies if requested
            if include_dependencies:
                dep_names = await self._registry.resolve_dependencies(name)
                dep_names = [n for n in dep_names if n != name]

                for dep_name in dep_names:
                    dep_skill = await self._registry.get_skill(dep_name, context=context)
                    total_size += await self._calculate_skill_size(dep_skill, context)

            if format == "claude" and total_size > 8 * 1024 * 1024:
                return False, "Skill export exceeds Claude format 8MB limit", total_size

            return True, "Export is valid", total_size

        except Exception as e:
            return False, f"Validation failed: {e}", 0

    async def _calculate_skill_size(self, skill: Skill, context: Any = None) -> int:
        """Calculate the total size of all files in a skill directory.

        Args:
            skill: Skill object
            context: Operation context for permission checks and file access

        Returns:
            Total size in bytes
        """
        if not skill.metadata.file_path:
            # Fallback to SKILL.md only if file_path not set
            content = self._reconstruct_skill_md(skill)
            return len(content.encode("utf-8"))

        # Get skill directory path (parent of SKILL.md)
        skill_dir_path = str(Path(skill.metadata.file_path).parent)

        # Get filesystem from registry
        if not self._registry._filesystem:
            # Fallback to SKILL.md only if filesystem not available
            content = self._reconstruct_skill_md(skill)
            return len(content.encode("utf-8"))

        filesystem = self._registry._filesystem

        # List all files in skill directory recursively
        try:
            all_files = filesystem.list(path=skill_dir_path, recursive=True, context=context)
        except Exception as e:
            logger.warning(f"Failed to list files for size calculation: {e}")
            # Fallback to SKILL.md only
            content = self._reconstruct_skill_md(skill)
            return len(content.encode("utf-8"))

        # Filter to only files (not directories) and exclude virtual parsed views
        file_paths = [
            f
            for f in all_files
            if isinstance(f, str)
            and not f.endswith("/")
            and not f.endswith("_parsed.md")
            and not f.endswith("_parsed.txt")
        ]

        total_size = 0
        for file_path in file_paths:
            try:
                raw_content = filesystem.read(file_path, context=context)
                assert isinstance(raw_content, bytes), "Expected bytes from read()"
                total_size += len(raw_content)
            except Exception as e:
                logger.warning(f"Failed to read file '{file_path}' for size calculation: {e}")

        return total_size

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
                # Extract all files from zip (preserving directory structure)
                for name in zip_file.namelist():
                    # Skip directories
                    if name.endswith("/"):
                        continue

                    # Extract to output directory
                    target_path = output_dir / name
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    content = zip_file.read(name)
                    target_path.write_bytes(content)

                    # Track skill names (from SKILL.md files)
                    if name.endswith("SKILL.md"):
                        skill_name = Path(name).parent.name
                        if skill_name not in imported_skills:
                            imported_skills.append(skill_name)
                            logger.info(f"Imported skill '{skill_name}' to {target_path}")
                    else:
                        logger.debug(f"Extracted file '{name}' to {target_path}")

        except Exception as e:
            raise SkillExportError(f"Failed to import skill package: {e}") from e

        return imported_skills
