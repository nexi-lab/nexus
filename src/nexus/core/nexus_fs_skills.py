"""Skills management operations for NexusFS.

This module provides RPC methods for skill operations. It's a thin delegation
layer to SkillService which contains the actual business logic.

## APIs

Distribution:
- skills_share: Grant read permission on a skill
- skills_unshare: Revoke read permission on a skill

Subscription:
- skills_discover: List skills the user has permission to see
- skills_subscribe: Add a skill to the user's library
- skills_unsubscribe: Remove a skill from the user's library

Runner:
- skills_get_prompt_context: Get skill metadata for system prompt injection
- skills_load: Load full skill content on-demand
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose
from nexus.services.skill_service import SkillService

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


class NexusFSSkillsMixin:
    """Mixin providing skills RPC methods for NexusFS.

    This is a thin delegation layer to SkillService. All business logic
    lives in the service.
    """

    _skill_service: SkillService | None = None

    def _get_skill_service(self) -> SkillService:
        """Get or create SkillService instance."""
        if self._skill_service is None:
            from nexus.services.gateway import NexusFSGateway

            gateway = NexusFSGateway(self)
            self._skill_service = SkillService(gateway=gateway)
        return self._skill_service

    # =========================================================================
    # Distribution APIs
    # =========================================================================

    @rpc_expose(description="Share a skill with users, groups, or make public")
    def skills_share(
        self,
        skill_path: str,
        share_with: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Grant read permission on a skill.
            self._skill_service = SkillService(
                nexus_fs=self,
                rebac_manager=getattr(self, "_rebac", None),
                cache=SkillCache(max_size=500, ttl_seconds=300),
            )
        Args:
            skill_path: Path to the skill (e.g., /tenant:acme/user:alice/skill/code-review/)
            share_with: Target to share with:
                - "public" - Make skill visible to everyone
                - "tenant" - Share with all users in current tenant
                - "group:<name>" - Share with a group
                - "user:<id>" - Share with a specific user
                - "agent:<id>" - Share with a specific agent
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with success, tuple_id, skill_path, share_with
        """
        service = self._get_skill_service()
        tuple_id = service.share(skill_path, share_with, context)
        return {
            "success": True,
            "tuple_id": tuple_id,
            "skill_path": skill_path,
            "share_with": share_with,
        }

    @rpc_expose(description="Revoke sharing permission on a skill")
    def skills_unshare(
        self,
        skill_path: str,
        unshare_from: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Revoke read permission on a skill.

        Args:
            skill_path: Path to the skill
            unshare_from: Target to unshare from (same format as share_with)
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with success, skill_path, unshare_from
        """
        service = self._get_skill_service()
        success = service.unshare(skill_path, unshare_from, context)
        return {
            "success": success,
            "skill_path": skill_path,
            "unshare_from": unshare_from,
        }

    # =========================================================================
    # Subscription APIs
    # =========================================================================

    @rpc_expose(description="Discover skills the user has permission to see")
    def skills_discover(
        self,
        filter: str = "all",
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List skills the user has permission to see.

        Args:
            filter: Filter mode:
                - "all" - All skills user can see
                - "public" - Only public skills
                - "subscribed" - Only skills in user's library
                - "owned" - Only skills owned by user
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with skills list and count
        """
        service = self._get_skill_service()
        skills = service.discover(context, filter)
        return {
            "skills": [s.to_dict() for s in skills],
            "count": len(skills),
        }

    @rpc_expose(description="Subscribe to a skill (add to user's library)")
    def skills_subscribe(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Subscribe to a skill.

        Args:
            skill_path: Path to the skill to subscribe to
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with success, skill_path, already_subscribed
        """
        service = self._get_skill_service()
        newly_subscribed = service.subscribe(skill_path, context)
        return {
            "success": True,
            "skill_path": skill_path,
            "already_subscribed": not newly_subscribed,
        }

    @rpc_expose(description="Unsubscribe from a skill (remove from user's library)")
    def skills_unsubscribe(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Unsubscribe from a skill.

        Args:
            skill_path: Path to the skill to unsubscribe from
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with success, skill_path, was_subscribed
        """
        service = self._get_skill_service()
        was_subscribed = service.unsubscribe(skill_path, context)
        return {
            "success": True,
            "skill_path": skill_path,
            "was_subscribed": was_subscribed,
        }

    # =========================================================================
    # Runner APIs
    # =========================================================================

    @rpc_expose(description="Get skill metadata for system prompt injection")
    def skills_get_prompt_context(
        self,
        max_skills: int = 50,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Get skill metadata formatted for system prompt injection.

        Args:
            max_skills: Maximum number of skills to include (default: 50)
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with xml, skills, count, token_estimate
        """
        service = self._get_skill_service()
        prompt_context = service.get_prompt_context(context, max_skills)
        return prompt_context.to_dict()

    @rpc_expose(description="Load full skill content on-demand")
    def skills_load(
        self,
        skill_path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Load full skill content.

        Args:
            skill_path: Path to the skill to load
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with name, path, owner, description, content, metadata
        """
        service = self._get_skill_service()
        content = service.load(skill_path, context)
        return content.to_dict()

    # =========================================================================
    # Package APIs (Import/Export)
    # =========================================================================

    @rpc_expose(description="Export a skill as a .skill (ZIP) package")
    def skills_export(
        self,
        skill_path: str | None = None,
        skill_name: str | None = None,
        output_path: str | None = None,
        format: str = "generic",
        _include_dependencies: bool = False,  # TODO: Implement dependency inclusion
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Export a skill to .skill (ZIP) format.

        Args:
            skill_path: Full path to the skill to export
            skill_name: Name of the skill (will search in user's skills)
            output_path: Optional path to write .skill file. If None, returns bytes.
            format: Export format ('generic' or 'claude')
            include_dependencies: Whether to include dependent skills
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with success, path (if written), or bytes (base64 if not written)
        """
        import base64
        import io
        import zipfile

        from nexus.core.exceptions import ValidationError

        service = self._get_skill_service()
        service._validate_context(context)

        # Resolve skill_path from skill_name if needed
        if not skill_path and skill_name:
            # Search for skill by name in user's skills
            user_skill_dir = f"/tenant:{context.tenant_id}/user:{context.user_id}/skill/"
            skill_path = f"{user_skill_dir}{skill_name}/"
            # Also check if it exists in subscribed skills
            if not self.exists(skill_path, context=context):
                # Try to find in all discoverable skills
                skills = service.discover(context, filter="all")
                for s in skills:
                    if s.name == skill_name:
                        skill_path = s.path
                        break

        if not skill_path:
            raise ValidationError("Either skill_path or skill_name must be provided")

        service._assert_can_read(skill_path, context)

        # Ensure path ends with /
        if not skill_path.endswith("/"):
            skill_path += "/"

        # Collect files from skill directory
        files_to_export: list[tuple[str, bytes]] = []

        def collect_files(dir_path: str, _prefix: str = "") -> None:
            try:
                items = self.list(dir_path, context=context)
                for item in items:
                    item_str = str(item)
                    if item_str.startswith(dir_path):
                        rel_path = item_str[len(dir_path) :]
                    else:
                        rel_path = item_str

                    full_path = (
                        f"{dir_path}{rel_path}" if not item_str.startswith("/") else item_str
                    )

                    # Check if it's a file by trying to read it
                    try:
                        content = self.read(full_path, context=context)
                        if isinstance(content, str):
                            content = content.encode("utf-8")
                        files_to_export.append((rel_path, content))
                    except Exception:
                        # Might be a directory, try to recurse
                        collect_files(full_path + "/", rel_path + "/")
            except Exception:
                pass

        collect_files(skill_path)

        if not files_to_export:
            raise ValidationError(f"No files found in skill: {skill_path}")

        # Create ZIP package
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Add manifest
            manifest = {
                "version": "1.0",
                "skill_path": skill_path,
                "files": [f[0] for f in files_to_export],
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            # Add skill files
            for rel_path, content in files_to_export:
                zf.writestr(rel_path, content)

        zip_bytes = zip_buffer.getvalue()

        # Extract skill name from path
        skill_name_from_path = skill_path.rstrip("/").split("/")[-1]

        if output_path:
            # Write to file using filesystem
            self.write(output_path, zip_bytes, context=context)
            return {
                "success": True,
                "path": output_path,
                "size_bytes": len(zip_bytes),
                "skill_name": skill_name_from_path,
                "format": format,
            }
        else:
            # Return base64 encoded bytes (frontend expects zip_data)
            return {
                "success": True,
                "zip_data": base64.b64encode(zip_bytes).decode("ascii"),
                "size_bytes": len(zip_bytes),
                "skill_name": skill_name_from_path,
                "format": format,
                "filename": f"{skill_name_from_path}.skill",
            }

    @rpc_expose(description="Import a skill from a .skill (ZIP) package")
    def skills_import(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,  # Alias for zip_bytes (frontend uses this name)
        target_path: str | None = None,
        allow_overwrite: bool = False,
        context: OperationContext | None = None,
        _tier: str | None = None,  # Legacy parameter (ignored)
    ) -> dict[str, Any]:
        """Import a skill from .skill (ZIP) format.

        Skills are always imported to the user's skill directory:
        /tenant:{tenant_id}/user:{user_id}/skill/{skill_name}/

        Args:
            source_path: Path to .skill file to import (either this or zip_bytes/zip_data)
            zip_bytes: ZIP bytes (base64 encoded string or raw bytes)
            zip_data: Alias for zip_bytes (base64 encoded string)
            target_path: Target path for the skill. If None, uses user's skill directory.
            allow_overwrite: Whether to overwrite existing skill
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with imported_skills, skill_paths
        """
        import base64
        import io
        import zipfile

        from nexus.core.exceptions import ValidationError

        service = self._get_skill_service()
        service._validate_context(context)

        # Use zip_data as alias for zip_bytes
        if zip_data and not zip_bytes:
            zip_bytes = zip_data

        # Get ZIP data
        if source_path:
            raw_zip_data = self.read(source_path, context=context)
            if isinstance(raw_zip_data, str):
                raw_zip_data = raw_zip_data.encode("utf-8")
        elif zip_bytes:
            raw_zip_data = base64.b64decode(zip_bytes) if isinstance(zip_bytes, str) else zip_bytes
        else:
            raise ValidationError("Either source_path or zip_data required")

        # Extract ZIP
        zip_buffer = io.BytesIO(raw_zip_data)
        files_imported: list[str] = []

        with zipfile.ZipFile(zip_buffer, mode="r") as zf:
            # Read manifest
            try:
                manifest_data = zf.read("manifest.json")
                manifest = json.loads(manifest_data.decode("utf-8"))
            except Exception:
                manifest = {}

            # Always import to user's skill directory
            base_path = f"/tenant:{context.tenant_id}/user:{context.user_id}/skill/"

            # Detect ZIP structure: flat (SKILL.md at root) or nested (skill-name/SKILL.md)
            file_list = zf.namelist()
            nested_skill_folder = None
            for name in file_list:
                if name.endswith("SKILL.md") and "/" in name:
                    # e.g., "my-skill/SKILL.md" -> "my-skill"
                    nested_skill_folder = name.split("/")[0]
                    break

            # Determine skill name from manifest, ZIP structure, or error
            skill_name = None
            if not target_path:
                manifest_skill_path = manifest.get("skill_path", "")
                if manifest_skill_path:
                    # Extract skill name from manifest path
                    skill_name = manifest_skill_path.rstrip("/").split("/")[-1]
                elif nested_skill_folder:
                    # Use folder name from ZIP structure
                    skill_name = nested_skill_folder
                else:
                    raise ValidationError(
                        "Cannot determine skill name. ZIP must contain SKILL.md in a named folder or have a manifest with skill_path."
                    )

                target_path = f"{base_path}{skill_name}/"

            if not target_path.endswith("/"):
                target_path += "/"

            # Check if skill exists and allow_overwrite
            skill_md_path = f"{target_path}SKILL.md"
            if self.exists(skill_md_path, context=context) and not allow_overwrite:
                raise ValidationError(
                    f"Skill already exists at {target_path}. Set allow_overwrite=true to overwrite."
                )

            # Extract skill name for response
            skill_name = target_path.rstrip("/").split("/")[-1]

            # Extract files, stripping the nested folder if present
            for name in file_list:
                if name == "manifest.json":
                    continue

                content = zf.read(name)

                if nested_skill_folder and name.startswith(nested_skill_folder + "/"):
                    # Strip the nested folder since target_path already includes skill name
                    rel_path = name[len(nested_skill_folder) + 1 :]
                else:
                    rel_path = name

                if rel_path and not rel_path.endswith("/"):  # Skip empty paths and folder entries
                    file_path = f"{target_path}{rel_path}"
                    self.write(file_path, content, context=context)
                    files_imported.append(file_path)

        # Return format expected by frontend
        return {
            "imported_skills": [skill_name],
            "skill_paths": [target_path],
        }

    @rpc_expose(description="Validate a .skill (ZIP) package")
    def skills_validate_zip(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        zip_data: str | None = None,  # Alias for zip_bytes (frontend uses this name)
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Validate a .skill (ZIP) package without importing it.

        Args:
            source_path: Path to .skill file to validate
            zip_bytes: ZIP bytes (base64 encoded string or raw bytes)
            zip_data: Alias for zip_bytes (base64 encoded string)
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with valid, skills_found, errors, warnings
        """
        import base64
        import io
        import zipfile

        from nexus.core.exceptions import ValidationError

        service = self._get_skill_service()
        service._validate_context(context)

        # Use zip_data as alias for zip_bytes
        if zip_data and not zip_bytes:
            zip_bytes = zip_data

        # Get ZIP data
        if source_path:
            raw_zip_data = self.read(source_path, context=context)
            if isinstance(raw_zip_data, str):
                raw_zip_data = raw_zip_data.encode("utf-8")
        elif zip_bytes:
            raw_zip_data = base64.b64decode(zip_bytes) if isinstance(zip_bytes, str) else zip_bytes
        else:
            raise ValidationError("Either source_path or zip_data required")

        errors: list[str] = []
        warnings: list[str] = []
        skills_found: list[str] = []
        has_skill_md = False

        try:
            zip_buffer = io.BytesIO(raw_zip_data)
            with zipfile.ZipFile(zip_buffer, mode="r") as zf:
                files = zf.namelist()

                # Check for manifest
                if "manifest.json" in files:
                    try:
                        manifest_data = zf.read("manifest.json")
                        manifest = json.loads(manifest_data.decode("utf-8"))
                        # Extract skill name from manifest
                        skill_path = manifest.get("skill_path", "")
                        if skill_path:
                            skill_name = skill_path.rstrip("/").split("/")[-1]
                            skills_found.append(skill_name)
                    except Exception as e:
                        errors.append(f"Invalid manifest.json: {e}")
                else:
                    warnings.append("Missing manifest.json (will use default skill name)")

                # Check for SKILL.md
                for f in files:
                    if f.endswith("SKILL.md") or f == "SKILL.md":
                        has_skill_md = True
                        # If no skill found from manifest, use SKILL.md parent folder
                        if not skills_found:
                            parts = f.rsplit("/", 1)
                            if len(parts) > 1:
                                skills_found.append(parts[0])
                            else:
                                skills_found.append("imported")
                        break

                if not has_skill_md:
                    errors.append("Missing SKILL.md file")

        except zipfile.BadZipFile as e:
            errors.append(f"Invalid ZIP file: {e}")
        except Exception as e:
            errors.append(f"Validation error: {e}")

        return {
            "valid": len(errors) == 0,
            "skills_found": skills_found,
            "errors": errors,
            "warnings": warnings,
        }
