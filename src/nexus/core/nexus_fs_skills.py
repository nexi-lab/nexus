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
        skill_path: str,
        output_path: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Export a skill to .skill (ZIP) format.

        Args:
            skill_path: Path to the skill to export
            output_path: Optional path to write .skill file. If None, returns bytes.
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
        service._assert_can_read(skill_path, context)

        # Ensure path ends with /
        if not skill_path.endswith("/"):
            skill_path += "/"

        # Collect files from skill directory
        files_to_export: list[tuple[str, bytes]] = []

        def collect_files(dir_path: str, prefix: str = "") -> None:
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

        if output_path:
            # Write to file using filesystem
            self.write(output_path, zip_bytes, context=context)
            return {"success": True, "path": output_path, "size": len(zip_bytes)}
        else:
            # Return base64 encoded bytes
            return {
                "success": True,
                "bytes": base64.b64encode(zip_bytes).decode("ascii"),
                "size": len(zip_bytes),
            }

    @rpc_expose(description="Import a skill from a .skill (ZIP) package")
    def skills_import(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        target_path: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Import a skill from .skill (ZIP) format.

        Args:
            source_path: Path to .skill file to import (either this or zip_bytes)
            zip_bytes: ZIP bytes (base64 encoded string or raw bytes)
            target_path: Target path for the skill. If None, uses manifest path.
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with success, skill_path, files_imported
        """
        import base64
        import io
        import zipfile

        from nexus.core.exceptions import ValidationError

        service = self._get_skill_service()
        service._validate_context(context)

        # Get ZIP data
        if source_path:
            zip_data = self.read(source_path, context=context)
            if isinstance(zip_data, str):
                zip_data = zip_data.encode("utf-8")
        elif zip_bytes:
            if isinstance(zip_bytes, str):
                zip_data = base64.b64decode(zip_bytes)
            else:
                zip_data = zip_bytes
        else:
            raise ValidationError("Either source_path or zip_bytes required")

        # Extract ZIP
        zip_buffer = io.BytesIO(zip_data)
        files_imported: list[str] = []

        with zipfile.ZipFile(zip_buffer, mode="r") as zf:
            # Read manifest
            try:
                manifest_data = zf.read("manifest.json")
                manifest = json.loads(manifest_data.decode("utf-8"))
            except Exception:
                manifest = {}

            # Determine target path
            if not target_path:
                target_path = manifest.get(
                    "skill_path",
                    f"/tenant:{context.tenant_id}/user:{context.user_id}/skill/imported/",
                )

            if not target_path.endswith("/"):
                target_path += "/"

            # Extract files
            for name in zf.namelist():
                if name == "manifest.json":
                    continue

                content = zf.read(name)
                file_path = f"{target_path}{name}"
                self.write(file_path, content, context=context)
                files_imported.append(file_path)

        return {
            "success": True,
            "skill_path": target_path,
            "files_imported": files_imported,
        }

    @rpc_expose(description="Validate a .skill (ZIP) package")
    def skills_validate_zip(
        self,
        source_path: str | None = None,
        zip_bytes: bytes | str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Validate a .skill (ZIP) package without importing it.

        Args:
            source_path: Path to .skill file to validate
            zip_bytes: ZIP bytes (base64 encoded string or raw bytes)
            context: Operation context with user_id and tenant_id

        Returns:
            Dict with valid, manifest, files, errors
        """
        import base64
        import io
        import zipfile

        from nexus.core.exceptions import ValidationError

        service = self._get_skill_service()
        service._validate_context(context)

        # Get ZIP data
        if source_path:
            zip_data = self.read(source_path, context=context)
            if isinstance(zip_data, str):
                zip_data = zip_data.encode("utf-8")
        elif zip_bytes:
            if isinstance(zip_bytes, str):
                zip_data = base64.b64decode(zip_bytes)
            else:
                zip_data = zip_bytes
        else:
            raise ValidationError("Either source_path or zip_bytes required")

        errors: list[str] = []
        manifest: dict[str, Any] = {}
        files: list[str] = []
        has_skill_md = False

        try:
            zip_buffer = io.BytesIO(zip_data)
            with zipfile.ZipFile(zip_buffer, mode="r") as zf:
                files = zf.namelist()

                # Check for manifest
                if "manifest.json" in files:
                    try:
                        manifest_data = zf.read("manifest.json")
                        manifest = json.loads(manifest_data.decode("utf-8"))
                    except Exception as e:
                        errors.append(f"Invalid manifest.json: {e}")
                else:
                    errors.append("Missing manifest.json")

                # Check for SKILL.md
                for f in files:
                    if f.endswith("SKILL.md") or f == "SKILL.md":
                        has_skill_md = True
                        break

                if not has_skill_md:
                    errors.append("Missing SKILL.md file")

        except zipfile.BadZipFile as e:
            errors.append(f"Invalid ZIP file: {e}")
        except Exception as e:
            errors.append(f"Validation error: {e}")

        return {
            "valid": len(errors) == 0,
            "manifest": manifest,
            "files": files,
            "errors": errors,
        }
