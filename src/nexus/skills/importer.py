"""Skill importer for ZIP/archive packages."""

import io
import logging
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from nexus.core.exceptions import PermissionDeniedError, ValidationError
from nexus.core.permissions import OperationContext
from nexus.skills.parser import SkillParseError, SkillParser
from nexus.skills.protocols import NexusFilesystem
from nexus.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


class SkillImportError(ValidationError):
    """Raised when skill import fails."""

    pass


class SkillImporter:
    """Import skills from ZIP/archive packages.

    Features:
    - Import from .zip/.skill packages (same format)
    - Validate skill structure before import
    - Check for name conflicts
    - Create ReBAC permissions
    - Support for system and user tiers

    Example:
        >>> from nexus import connect
        >>> from nexus.skills import SkillRegistry, SkillImporter
        >>>
        >>> nx = connect()
        >>> registry = SkillRegistry(nx)
        >>> importer = SkillImporter(nx, registry)
        >>>
        >>> # Import from ZIP bytes
        >>> result = await importer.import_from_zip(
        ...     zip_data=zip_bytes,
        ...     tier="user",
        ...     context=context
        ... )
        >>> print(result["imported_skills"])
        ['my-skill']
    """

    # Valid skill name pattern (alphanumeric, hyphens, underscores)
    VALID_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

    def __init__(
        self,
        filesystem: NexusFilesystem,
        registry: SkillRegistry | None = None,
    ):
        """Initialize skill importer.

        Args:
            filesystem: Nexus filesystem instance
            registry: Optional skill registry for conflict checking
        """
        self._filesystem = filesystem
        self._registry = registry or SkillRegistry(filesystem)
        self._parser = SkillParser()

    async def import_from_zip(
        self,
        zip_data: bytes,
        tier: str = "user",
        allow_overwrite: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Import skill from ZIP package.

        Process:
        1. Extract ZIP to temporary directory
        2. Validate structure (must have skill-name/SKILL.md)
        3. Parse and validate SKILL.md frontmatter
        4. Check for name conflicts in target tier
        5. Copy to target tier path
        6. Create ReBAC permissions
        7. Update skill registry

        Args:
            zip_data: ZIP file bytes
            tier: Target tier (personal/tenant/system)
            allow_overwrite: Allow overwriting existing skills
            context: Operation context with user_id, tenant_id

        Returns:
            {
                "imported_skills": ["skill-name"],
                "skill_paths": [
                    "/tenant:<tid>/user:<uid>/skill/<skill-name>/" for personal,
                    "/tenant:<tid>/skill/<skill-name>/" for tenant,
                    "/skill/<skill-name>/" for system
                ],
                "tier": "personal" | "tenant" | "system"
            }

        Raises:
            SkillImportError: If import fails
            PermissionDeniedError: If insufficient permissions
        """
        # Permission check for system tier
        if tier == "system" and (not context or not getattr(context, "is_admin", False)):
            raise PermissionDeniedError("Only admins can import to system tier")

        # Extract and validate ZIP
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            try:
                # Extract ZIP
                with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zip_ref:
                    zip_ref.extractall(tmpdir_path)
                    logger.info(f"Extracted ZIP to {tmpdir_path}")
            except zipfile.BadZipFile as e:
                raise SkillImportError(f"Invalid ZIP file: {e}") from e
            except Exception as e:
                raise SkillImportError(f"Failed to extract ZIP: {e}") from e

            # Validate structure
            valid, skill_names, errors = self._validate_skill_structure(tmpdir_path)

            if not valid:
                error_msg = "Invalid skill package: " + "; ".join(errors)
                raise SkillImportError(error_msg)

            if not skill_names:
                raise SkillImportError("No valid skills found in ZIP package")

            # Check for conflicts
            conflicts = []
            for skill_name in skill_names:
                if (
                    await self._check_name_conflict(skill_name, tier, context)
                    and not allow_overwrite
                ):
                    conflicts.append(skill_name)

            if conflicts:
                conflict_msg = ", ".join(conflicts)
                raise SkillImportError(
                    f"Skill(s) already exist: {conflict_msg}. Please rename or enable overwrite."
                )

            # Import skills
            imported_skills = []
            skill_paths = []

            for skill_name in skill_names:
                skill_dir = self._find_skill_directory(tmpdir_path, skill_name)
                if not skill_dir:
                    logger.warning(f"Could not find directory for skill: {skill_name}")
                    continue

                # Get target path
                target_path = self._get_target_path(skill_name, tier, context)

                # Copy skill directory to target
                await self._copy_skill_directory(skill_dir, target_path, context)

                imported_skills.append(skill_name)
                skill_paths.append(target_path)

                logger.info(f"Imported skill '{skill_name}' to {target_path}")

            # Refresh registry
            if self._registry:
                await self._registry.discover(context)

            return {
                "imported_skills": imported_skills,
                "skill_paths": skill_paths,
                "tier": tier,
            }

    async def validate_zip(self, zip_data: bytes) -> dict[str, Any]:
        """Validate skill ZIP package without importing.

        Args:
            zip_data: ZIP file bytes

        Returns:
            {
                "valid": bool,
                "skills_found": ["skill-name"],
                "errors": ["error message"],
                "warnings": ["warning message"]
            }
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)

            # Extract ZIP
            try:
                with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zip_ref:
                    zip_ref.extractall(tmpdir_path)
            except zipfile.BadZipFile as e:
                return {
                    "valid": False,
                    "skills_found": [],
                    "errors": [f"Invalid ZIP file: {e}"],
                    "warnings": [],
                }
            except Exception as e:
                return {
                    "valid": False,
                    "skills_found": [],
                    "errors": [f"Failed to extract ZIP: {e}"],
                    "warnings": [],
                }

            # Validate structure
            valid, skill_names, errors = self._validate_skill_structure(tmpdir_path)

            # Collect warnings
            warnings = []
            for skill_name in skill_names:
                skill_dir = self._find_skill_directory(tmpdir_path, skill_name)
                if skill_dir:
                    skill_md = skill_dir / "SKILL.md"
                    try:
                        metadata = self._parser.parse_metadata_only(skill_md)
                        if not metadata.version:
                            warnings.append(f"Missing version in '{skill_name}'")
                        if not metadata.author:
                            warnings.append(f"Missing author in '{skill_name}'")
                    except Exception as e:
                        logger.debug(f"Could not parse metadata for warnings: {e}")

            return {
                "valid": valid,
                "skills_found": skill_names,
                "errors": errors,
                "warnings": warnings,
            }

    def _validate_skill_structure(self, extracted_path: Path) -> tuple[bool, list[str], list[str]]:
        """Validate that extracted ZIP has proper skill structure.

        Expected structure:
            skill-name/
                SKILL.md (required)
                other-files/ (optional)

        Args:
            extracted_path: Path to extracted ZIP contents

        Returns:
            (valid, skill_names, errors)
        """
        errors = []
        skill_names = []

        # Find all SKILL.md files
        skill_md_files = list(extracted_path.rglob("SKILL.md"))

        if not skill_md_files:
            errors.append("No SKILL.md file found in package")
            return False, [], errors

        for skill_md in skill_md_files:
            # Get skill directory (parent of SKILL.md)
            skill_dir = skill_md.parent

            # Check that SKILL.md is at root of a skill directory
            # (not nested deeper, e.g., skill-name/SKILL.md is OK,
            # but skill-name/subdir/SKILL.md is not)
            relative_path = skill_dir.relative_to(extracted_path)
            path_parts = relative_path.parts

            # Should be exactly one level deep (skill-name/)
            if len(path_parts) != 1:
                errors.append(
                    f"SKILL.md must be at root of skill directory, "
                    f"found at: {relative_path}/SKILL.md"
                )
                continue

            skill_name = path_parts[0]

            # Validate skill name format
            if not self.VALID_NAME_PATTERN.match(skill_name):
                errors.append(
                    f"Invalid skill name '{skill_name}': "
                    f"must be alphanumeric with hyphens or underscores only"
                )
                continue

            # Parse and validate SKILL.md
            try:
                metadata = self._parser.parse_metadata_only(skill_md)

                # Verify name matches directory name
                if metadata.name != skill_name:
                    errors.append(
                        f"Skill name mismatch: directory is '{skill_name}' "
                        f"but SKILL.md name is '{metadata.name}'"
                    )
                    continue

                # Validate required fields
                if not metadata.description:
                    errors.append(f"Missing description in skill '{skill_name}'")
                    continue

                skill_names.append(skill_name)

            except SkillParseError as e:
                errors.append(f"Invalid SKILL.md in '{skill_name}': {e}")
                continue
            except Exception as e:
                errors.append(f"Failed to parse SKILL.md in '{skill_name}': {e}")
                continue

        valid = len(errors) == 0 and len(skill_names) > 0

        return valid, skill_names, errors

    async def _check_name_conflict(
        self,
        skill_name: str,
        tier: str,
        context: OperationContext | None,
    ) -> bool:
        """Check if skill name already exists in target tier.

        Args:
            skill_name: Name of skill to check
            tier: Target tier (user/system)
            context: Operation context

        Returns:
            True if conflict exists, False otherwise
        """
        target_path = self._get_target_path(skill_name, tier, context)

        try:
            # Check if directory exists
            return self._filesystem.exists(target_path)
        except Exception:
            # Path doesn't exist, no conflict
            return False

    def _get_target_path(
        self,
        skill_name: str,
        tier: str,
        context: OperationContext | None,
    ) -> str:
        """Get target path for skill based on tier and context.

        Args:
            skill_name: Name of skill
            tier: Target tier (personal/tenant/system)
            context: Operation context with tenant_id and user_id

        Returns:
            Target path string:
            - personal: /tenant:<tid>/user:<uid>/skill/<skill_name>/
            - tenant: /tenant:<tid>/skill/<skill_name>/
            - system: /skill/<skill_name>/
        """
        if tier == "system":
            return f"/skill/{skill_name}/"

        if not context:
            raise ValueError("Context required for personal/tenant tier skills")

        tenant_id = context.tenant_id or "default"

        if tier == "personal":
            # Personal: /tenant:<tid>/user:<uid>/skill/<skill_name>/
            user_id = context.user_id or getattr(context, "user", None)
            if not user_id:
                raise ValueError("user_id required for personal tier skills")
            return f"/tenant:{tenant_id}/user:{user_id}/skill/{skill_name}/"

        if tier == "tenant":
            # Tenant: /tenant:<tid>/skill/<skill_name>/
            return f"/tenant:{tenant_id}/skill/{skill_name}/"

        # Legacy user tier support (for backward compatibility)
        if tier == "user":
            user_id = context.user_id or getattr(context, "user", None)
            if user_id:
                return f"/skills/users/{user_id}/{skill_name}/"
            return f"/skills/user/{skill_name}/"

        raise ValueError(f"Unknown tier: {tier}. Must be 'personal', 'tenant', or 'system'")

    def _find_skill_directory(self, extracted_path: Path, skill_name: str) -> Path | None:
        """Find skill directory in extracted ZIP.

        Args:
            extracted_path: Path to extracted ZIP
            skill_name: Name of skill to find

        Returns:
            Path to skill directory or None
        """
        skill_dir = extracted_path / skill_name
        if skill_dir.exists() and skill_dir.is_dir():
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                return skill_dir

        return None

    async def _copy_skill_directory(
        self, source_dir: Path, target_path: str, context: OperationContext | None = None
    ) -> None:
        """Copy skill directory to target path in filesystem.

        Args:
            source_dir: Local path to skill directory
            target_path: Target path in Nexus filesystem
            context: Operation context for permission checks
        """
        # Create target directory
        self._filesystem.mkdir(target_path, parents=True, exist_ok=True)

        # Copy all files recursively
        for item in source_dir.rglob("*"):
            if item.is_file():
                relative_path = item.relative_to(source_dir)
                target_file_path = f"{target_path}{relative_path}"

                # Read file content
                content = item.read_bytes()

                # Write to filesystem
                self._filesystem.write(target_file_path, content, context=context)

                logger.debug(f"Copied {item} to {target_file_path}")
