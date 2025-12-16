"""Skills management operations for NexusFS.

This module contains skills management operations exposed via RPC:
- skills_create: Create a new skill from template
- skills_create_from_content: Create a skill from web content
- skills_create_from_file: Create skill from file or URL (auto-detects type)
- skills_list: List all skills
- skills_info: Get detailed skill information
- skills_fork: Fork an existing skill
- skills_publish: Publish skill to another tier
- skills_search: Search skills by description
- skills_import: Import skill from .zip/.skill package
- skills_validate_zip: Validate skill ZIP package without importing
- skills_export: Export skill to .zip package
- skills_validate: Validate skill format
- skills_submit_approval: Submit skill for approval
- skills_approve: Approve a skill
- skills_reject: Reject a skill
- skills_list_approvals: List approval requests
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.exceptions import ValidationError
from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


class NexusFSSkillsMixin:
    """Mixin providing skills management operations for NexusFS."""

    def _run_async_skill_operation(self, coro: Any) -> dict[str, Any]:
        """Run an async skill operation in the current or new event loop.

        Args:
            coro: Coroutine to run

        Returns:
            Result from the coroutine
        """
        try:
            # Try to get the current running loop
            loop = asyncio.get_running_loop()
            # If we're already in an async context, we can't use run_until_complete
            # Instead, we need to schedule it differently
            # For now, create a new thread with a new loop
            import threading

            result_holder: list[Any] = []
            exception_holder: list[Exception] = []

            def run_in_thread() -> None:
                try:
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    result = new_loop.run_until_complete(coro)
                    result_holder.append(result)
                except Exception as e:
                    exception_holder.append(e)
                finally:
                    new_loop.close()

            thread = threading.Thread(target=run_in_thread)
            thread.start()
            thread.join()

            if exception_holder:
                raise exception_holder[0]
            if result_holder:
                return result_holder[0]  # type: ignore[no-any-return]
            return {}

        except RuntimeError:
            # No running loop - create one and run
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

    def _get_skill_registry(self) -> Any:
        """Get or create SkillRegistry instance.

        Returns:
            SkillRegistry instance
        """
        from typing import cast

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills import SkillRegistry

        return SkillRegistry(cast(NexusFilesystem, self))

    def _get_skill_manager(self) -> Any:
        """Get or create SkillManager instance.

        Returns:
            SkillManager instance
        """
        from typing import cast

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills import SkillManager

        registry = self._get_skill_registry()
        return SkillManager(cast(NexusFilesystem, self), registry)

    def _get_skill_governance(self) -> Any:
        """Get or create SkillGovernance instance.

        Returns:
            SkillGovernance instance
        """
        from nexus.skills import SkillGovernance

        # Get database connection if available
        db_conn = None
        if (
            hasattr(self, "metadata_store")
            and self.metadata_store
            and hasattr(self.metadata_store, "session")
        ):
            from nexus.cli.commands.skills import SQLAlchemyDatabaseConnection

            db_conn = SQLAlchemyDatabaseConnection(self.metadata_store.session)

        return SkillGovernance(db_connection=db_conn)

    @rpc_expose(description="Create a new skill from template")
    def skills_create(
        self,
        name: str,
        description: str,
        template: str = "basic",
        tier: str = "user",
        author: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Create a new skill from template.

        Args:
            name: Skill name
            description: Skill description
            template: Template name (default: "basic")
            tier: Target tier (agent/user/tenant/system, default: "user")
            author: Optional author name
            context: Operation context with user_id, tenant_id

        Returns:
            Dict with skill_path, name, tier, template
        """
        manager = self._get_skill_manager()

        async def create() -> dict[str, Any]:
            skill_path = await manager.create_skill(
                name=name,
                description=description,
                template=template,
                tier=tier,
                author=author,
                context=context,
            )
            return {
                "skill_path": skill_path,
                "name": name,
                "tier": tier,
                "template": template,
            }

        return self._run_async_skill_operation(create())

    @rpc_expose(description="Create a skill from web content")
    def skills_create_from_content(
        self,
        name: str,
        description: str,
        content: str,
        tier: str = "user",
        author: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Create a skill from custom content.

        Args:
            name: Skill name
            description: Skill description
            content: Skill markdown content
            tier: Target tier (default: "user")
            author: Optional author name
            source_url: Optional source URL
            metadata: Optional additional metadata
            context: Operation context with user_id, tenant_id

        Returns:
            Dict with skill_path, name, tier, source_url
        """
        manager = self._get_skill_manager()

        async def create() -> dict[str, Any]:
            skill_path = await manager.create_skill_from_content(
                name=name,
                description=description,
                content=content,
                tier=tier,
                author=author,
                source_url=source_url,
                metadata=metadata,
                context=context,
            )
            return {
                "skill_path": skill_path,
                "name": name,
                "tier": tier,
                "source_url": source_url,
            }

        return self._run_async_skill_operation(create())

    @rpc_expose(description="Create skill from file or URL (auto-detects type)")
    def skills_create_from_file(
        self,
        source: str,
        file_data: str | None = None,
        name: str | None = None,
        description: str | None = None,
        tier: str = "agent",
        use_ai: bool = False,
        use_ocr: bool = False,
        extract_tables: bool = False,
        extract_images: bool = False,
        _author: str | None = None,  # Unused: plugin manages authorship
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Create a skill from file or URL (auto-detects type).

        Args:
            source: File path or URL
            file_data: Base64 encoded file data (for remote calls)
            name: Skill name (auto-generated if not provided)
            description: Skill description
            tier: Target tier (agent, tenant, system)
            use_ai: Enable AI enhancement
            use_ocr: Enable OCR for scanned PDFs
            extract_tables: Extract tables from documents
            extract_images: Extract images from documents
            author: Author name
            context: Operation context

        Returns:
            Dict with skill_path, name, tier, source
        """
        import base64
        import tempfile
        from pathlib import Path
        from urllib.parse import urlparse

        # Load plugin
        try:
            from nexus_skill_seekers.plugin import SkillSeekersPlugin

            plugin = SkillSeekersPlugin(nexus_fs=self)
        except ImportError as e:
            raise RuntimeError(
                "skill-seekers plugin not installed. "
                "Install with: pip install nexus-plugin-skill-seekers"
            ) from e

        # Detect source type
        is_url = source.startswith(("http://", "https://"))
        is_pdf = source.lower().endswith(".pdf")

        # Auto-generate name if not provided
        if not name:
            if is_url:
                parsed = urlparse(source)
                name = parsed.path.strip("/").split("/")[-1] or parsed.netloc
                name = name.lower().replace(".", "-").replace("_", "-")
            else:
                name = Path(source).stem.lower().replace(" ", "-").replace("_", "-")

        async def create() -> dict[str, Any]:
            skill_path: str | None = None

            # Handle file data (for remote calls)
            if file_data:
                # Decode base64 and write to temp file
                decoded = base64.b64decode(file_data)
                with tempfile.NamedTemporaryFile(delete=False, suffix=Path(source).suffix) as tmp:
                    tmp.write(decoded)
                    tmp_path = tmp.name

                try:
                    if is_pdf:
                        skill_path = await plugin.generate_skill_from_pdf(
                            pdf_path=tmp_path,
                            name=name,
                            tier=tier,
                            description=description,
                            use_ai=use_ai,
                            use_ocr=use_ocr,
                            extract_tables=extract_tables,
                            extract_images=extract_images,
                        )
                finally:
                    # Clean up temp file
                    Path(tmp_path).unlink(missing_ok=True)
            elif is_pdf:
                # Local file path
                skill_path = await plugin.generate_skill_from_pdf(
                    pdf_path=source,
                    name=name,
                    tier=tier,
                    description=description,
                    use_ai=use_ai,
                    use_ocr=use_ocr,
                    extract_tables=extract_tables,
                    extract_images=extract_images,
                )
            elif is_url:
                # URL scraping
                skill_path = await plugin.generate_skill(
                    url=source,
                    name=name,
                    tier=tier,
                    description=description,
                    use_ai=use_ai,
                )
            else:
                raise ValueError(f"Unsupported source type: {source}")

            if not skill_path:
                raise RuntimeError("Failed to generate skill")

            return {
                "skill_path": skill_path,
                "name": name,
                "tier": tier,
                "source": source,
            }

        return self._run_async_skill_operation(create())

    @rpc_expose(description="List all skills")
    def skills_list(
        self,
        tier: str | None = None,
        include_metadata: bool = True,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List all skills.

        Args:
            tier: Filter by tier (agent/tenant/system)
            include_metadata: Include full metadata (default: True)
            context: Operation context

        Returns:
            Dict with skills list
        """
        registry = self._get_skill_registry()

        async def list_skills() -> dict[str, Any]:
            await registry.discover(context=context)
            skills = registry.list_skills(tier=tier, include_metadata=include_metadata)

            # Convert SkillMetadata objects to dicts
            skills_data = []
            for skill in skills:
                if hasattr(skill, "__dict__"):
                    # It's a SkillMetadata object
                    skill_dict = {
                        "name": skill.name,
                        "description": skill.description,
                        "version": skill.version,
                        "author": skill.author,
                        "tier": skill.tier,
                        "file_path": skill.file_path,
                        "requires": skill.requires,
                    }
                    if skill.created_at:
                        skill_dict["created_at"] = skill.created_at.isoformat()
                    if skill.modified_at:
                        skill_dict["modified_at"] = skill.modified_at.isoformat()
                    skills_data.append(skill_dict)
                else:
                    # It's already a string (skill name)
                    skills_data.append(skill)

            return {"skills": skills_data, "count": len(skills_data)}

        return self._run_async_skill_operation(list_skills())

    @rpc_expose(description="Get detailed skill information")
    def skills_info(
        self,
        skill_name: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Get detailed skill information.

        Args:
            skill_name: Name of the skill
            context: Operation context

        Returns:
            Dict with skill metadata and dependencies
        """
        registry = self._get_skill_registry()

        async def get_info() -> dict[str, Any]:
            await registry.discover(context=context)
            metadata = registry.get_metadata(skill_name)

            skill_info = {
                "name": metadata.name,
                "description": metadata.description,
                "version": metadata.version,
                "author": metadata.author,
                "tier": metadata.tier,
                "file_path": metadata.file_path,
                "requires": metadata.requires,
            }

            if metadata.created_at:
                skill_info["created_at"] = metadata.created_at.isoformat()
            if metadata.modified_at:
                skill_info["modified_at"] = metadata.modified_at.isoformat()

            # Add resolved dependencies
            if metadata.requires:
                resolved = await registry.resolve_dependencies(skill_name)
                skill_info["resolved_dependencies"] = resolved

            return skill_info

        return self._run_async_skill_operation(get_info())

    @rpc_expose(description="Fork an existing skill")
    def skills_fork(
        self,
        source_name: str,
        target_name: str,
        tier: str = "agent",
        author: str | None = None,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Fork an existing skill.

        Args:
            source_name: Source skill name
            target_name: Target skill name
            tier: Target tier (default: "agent")
            author: Optional author name
            context: Operation context

        Returns:
            Dict with forked_path, source_name, target_name, tier
        """
        manager = self._get_skill_manager()
        registry = self._get_skill_registry()

        async def fork() -> dict[str, Any]:
            await registry.discover(context=context)
            forked_path = await manager.fork_skill(
                source_name=source_name,
                target_name=target_name,
                tier=tier,
                author=author,
            )
            return {
                "forked_path": forked_path,
                "source_name": source_name,
                "target_name": target_name,
                "tier": tier,
            }

        return self._run_async_skill_operation(fork())

    @rpc_expose(description="Publish skill to another tier")
    def skills_publish(
        self,
        skill_name: str,
        source_tier: str = "agent",
        target_tier: str = "tenant",
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Publish skill to another tier.

        Args:
            skill_name: Skill name
            source_tier: Source tier (default: "agent")
            target_tier: Target tier (default: "tenant")
            context: Operation context

        Returns:
            Dict with published_path, skill_name, source_tier, target_tier
        """
        manager = self._get_skill_manager()

        async def publish() -> dict[str, Any]:
            published_path = await manager.publish_skill(
                name=skill_name,
                source_tier=source_tier,
                target_tier=target_tier,
            )
            return {
                "published_path": published_path,
                "skill_name": skill_name,
                "source_tier": source_tier,
                "target_tier": target_tier,
            }

        return self._run_async_skill_operation(publish())

    @rpc_expose(description="Search skills by description")
    def skills_search(
        self,
        query: str,
        tier: str | None = None,
        limit: int = 10,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Search skills by description.

        Args:
            query: Search query
            tier: Filter by tier
            limit: Maximum results (default: 10)
            context: Operation context

        Returns:
            Dict with results list
        """
        manager = self._get_skill_manager()

        async def search() -> dict[str, Any]:
            results = await manager.search_skills(query=query, tier=tier, limit=limit)
            # Convert to serializable format
            results_data = [{"skill_name": name, "score": score} for name, score in results]
            return {"results": results_data, "query": query, "count": len(results_data)}

        return self._run_async_skill_operation(search())

    @rpc_expose(description="Submit skill for approval")
    def skills_submit_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: list[str] | None = None,
        comments: str | None = None,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Submit a skill for approval.

        Args:
            skill_name: Skill name
            submitted_by: Submitter ID
            reviewers: Optional list of reviewer IDs
            comments: Optional submission comments
            context: Operation context

        Returns:
            Dict with approval_id, skill_name, submitted_by
        """
        governance = self._get_skill_governance()

        async def submit() -> dict[str, Any]:
            approval_id = await governance.submit_for_approval(
                skill_name=skill_name,
                submitted_by=submitted_by,
                reviewers=reviewers,
                comments=comments,
            )
            return {
                "approval_id": approval_id,
                "skill_name": skill_name,
                "submitted_by": submitted_by,
                "reviewers": reviewers,
            }

        return self._run_async_skill_operation(submit())

    @rpc_expose(description="Approve a skill")
    def skills_approve(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        tenant_id: str | None = None,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Approve a skill for publication.

        Args:
            approval_id: Approval request ID
            reviewed_by: Reviewer ID
            reviewer_type: Reviewer type (user/agent, default: "user")
            comments: Optional review comments
            tenant_id: Optional tenant ID
            context: Operation context

        Returns:
            Dict with approval_id, reviewed_by, status
        """
        governance = self._get_skill_governance()

        async def approve() -> dict[str, Any]:
            await governance.approve_skill(
                approval_id=approval_id,
                reviewed_by=reviewed_by,
                reviewer_type=reviewer_type,
                comments=comments,
                tenant_id=tenant_id,
            )
            return {
                "approval_id": approval_id,
                "reviewed_by": reviewed_by,
                "reviewer_type": reviewer_type,
                "status": "approved",
            }

        return self._run_async_skill_operation(approve())

    @rpc_expose(description="Reject a skill")
    def skills_reject(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        tenant_id: str | None = None,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Reject a skill for publication.

        Args:
            approval_id: Approval request ID
            reviewed_by: Reviewer ID
            reviewer_type: Reviewer type (user/agent, default: "user")
            comments: Optional rejection reason
            tenant_id: Optional tenant ID
            context: Operation context

        Returns:
            Dict with approval_id, reviewed_by, status
        """
        governance = self._get_skill_governance()

        async def reject() -> dict[str, Any]:
            await governance.reject_skill(
                approval_id=approval_id,
                reviewed_by=reviewed_by,
                reviewer_type=reviewer_type,
                comments=comments,
                tenant_id=tenant_id,
            )
            return {
                "approval_id": approval_id,
                "reviewed_by": reviewed_by,
                "reviewer_type": reviewer_type,
                "status": "rejected",
            }

        return self._run_async_skill_operation(reject())

    @rpc_expose(description="List approval requests")
    def skills_list_approvals(
        self,
        status: str | None = None,
        skill_name: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List skill approval requests.

        Args:
            status: Filter by status (pending/approved/rejected)
            skill_name: Filter by skill name
            context: Operation context

        Returns:
            Dict with approvals list
        """
        governance = self._get_skill_governance()

        async def list_approvals() -> dict[str, Any]:
            approvals = await governance.list_approvals(status=status, skill_name=skill_name)

            # Convert to serializable format
            approvals_data = []
            for approval in approvals:
                approval_dict = {
                    "approval_id": approval.approval_id,
                    "skill_name": approval.skill_name,
                    "status": approval.status.value,
                    "submitted_by": approval.submitted_by,
                }
                if approval.submitted_at:
                    approval_dict["submitted_at"] = approval.submitted_at.isoformat()
                if approval.reviewed_by:
                    approval_dict["reviewed_by"] = approval.reviewed_by
                if approval.reviewed_at:
                    approval_dict["reviewed_at"] = approval.reviewed_at.isoformat()
                if approval.comments:
                    approval_dict["comments"] = approval.comments
                approvals_data.append(approval_dict)

            return {"approvals": approvals_data, "count": len(approvals_data)}

        return self._run_async_skill_operation(list_approvals())

    @rpc_expose(description="Import skill from .zip/.skill package")
    def skills_import(
        self,
        zip_data: str,
        tier: str = "user",
        allow_overwrite: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Import skill from ZIP package.

        Args:
            zip_data: Base64 encoded ZIP file bytes
            tier: Target tier (personal/tenant/system)
            allow_overwrite: Allow overwriting existing skills
            context: Operation context with user_id, tenant_id

        Returns:
            {
                "imported_skills": ["skill-name"],
                "skill_paths": ["/tenant:<tid>/user:<uid>/skill/<skill_name>/"],
                "tier": "personal"
            }

        Raises:
            ValidationError: Invalid ZIP structure or skill format
            PermissionDeniedError: Insufficient permissions
        """
        import base64

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills.importer import SkillImporter

        # Permission check: system tier requires admin (users cannot add system skills)
        if tier == "system" and context and not getattr(context, "is_admin", False):
            from nexus.core.exceptions import PermissionDeniedError

            raise PermissionDeniedError("Only admins can import to system tier")

        # Decode base64 ZIP data
        zip_bytes = base64.b64decode(zip_data)

        # Get importer
        from typing import cast

        registry = self._get_skill_registry()
        importer = SkillImporter(cast(NexusFilesystem, self), registry)

        # Import skill
        async def import_skill() -> dict[str, Any]:
            return await importer.import_from_zip(
                zip_data=zip_bytes,
                tier=tier,
                allow_overwrite=allow_overwrite,
                context=context,
            )

        return self._run_async_skill_operation(import_skill())

    @rpc_expose(description="Validate skill ZIP package without importing")
    def skills_validate_zip(
        self,
        zip_data: str,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Validate skill ZIP package.

        Args:
            zip_data: Base64 encoded ZIP file bytes
            context: Operation context

        Returns:
            {
                "valid": bool,
                "skills_found": ["skill-name"],
                "errors": ["error message"],
                "warnings": ["warning message"]
            }
        """
        import base64

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills.importer import SkillImporter

        zip_bytes = base64.b64decode(zip_data)

        from typing import cast

        registry = self._get_skill_registry()
        importer = SkillImporter(cast(NexusFilesystem, self), registry)

        async def validate() -> dict[str, Any]:
            return await importer.validate_zip(zip_bytes)

        return self._run_async_skill_operation(validate())

    @rpc_expose(description="Export skill to .skill package")
    def skills_export(
        self,
        skill_name: str,
        include_dependencies: bool = False,
        format: str = "generic",
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Export skill to .skill (ZIP) package.

        Args:
            skill_name: Name of skill to export
            include_dependencies: Include skill dependencies
            context: Operation context

        Returns:
            {
                "skill_name": str,
                "zip_data": str,  # Base64 encoded ZIP file
                "size_bytes": int,
                "filename": str  # Suggested filename (e.g., "skill-name.skill")
            }
        """
        import base64

        from nexus.skills.exporter import SkillExporter

        registry = self._get_skill_registry()
        exporter = SkillExporter(registry)

        async def export() -> dict[str, Any]:
            await registry.discover(context=context)

            # Export to bytes
            zip_bytes = await exporter.export_skill(
                name=skill_name,
                output_path=None,  # Return bytes
                include_dependencies=include_dependencies,
                format=format,
                context=context,
            )

            # Check if export succeeded
            if zip_bytes is None:
                raise ValidationError(f"Failed to export skill '{skill_name}'")

            # Encode to base64
            zip_base64 = base64.b64encode(zip_bytes).decode("utf-8")

            return {
                "skill_name": skill_name,
                "zip_data": zip_base64,
                "size_bytes": len(zip_bytes),
                "filename": f"{skill_name}.skill",  # Suggested filename with .skill extension (ZIP format)
            }

        return self._run_async_skill_operation(export())
