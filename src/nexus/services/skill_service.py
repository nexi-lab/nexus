"""Skill Service - Extracted from NexusFSSkillsMixin.

This service handles all skill lifecycle management operations:
- Create skills from templates, content, files, or URLs
- List, search, and get detailed skill information
- Fork and publish skills across tiers
- Import/export skills as packages
- Skill approval workflow (submit, approve, reject)

Phase 2: Core Refactoring (Issue #988, Task 2.7)
Extracted from: nexus_fs_skills.py (874 lines)
"""

from __future__ import annotations

import builtins
import logging
from typing import TYPE_CHECKING, Any

from nexus.core.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


class SkillService:
    """Independent skill service extracted from NexusFS.

    Handles all skill lifecycle management operations:
    - Skill creation from various sources (templates, content, files, URLs)
    - Skill discovery and metadata management
    - Cross-tier skill publishing and forking
    - Package import/export (.skill/.zip packages)
    - Governance and approval workflow

    Architecture:
        - Works with SkillRegistry for skill discovery
        - Uses SkillManager for creation and management
        - Integrates SkillGovernance for approval workflow
        - Supports multi-tier isolation (agent/user/tenant/system)
        - Clean dependency injection

    Example:
        ```python
        skill_service = SkillService(
            skill_registry=registry,
            skill_manager=manager,
            skill_governance=governance
        )

        # Create skill from template
        result = skill_service.skills_create(
            name="data-analyzer",
            description="Analyze CSV data",
            template="basic",
            tier="user"
        )

        # List all skills
        skills = skill_service.skills_list(tier="user")

        # Create skill from URL
        result = skill_service.skills_create_from_file(
            source="https://example.com/docs",
            tier="agent",
            use_ai=True
        )

        # Fork a skill
        fork_result = skill_service.skills_fork(
            source_path="/skills/user/original",
            new_name="my-fork",
            tier="user"
        )

        # Export skill as package
        package_path = skill_service.skills_export(
            skill_path="/skills/user/my-skill",
            output_path="/exports/my-skill.skill"
        )
        ```
    """

    def __init__(
        self,
        skill_registry: Any | None = None,
        skill_manager: Any | None = None,
        skill_governance: Any | None = None,
        nexus_fs: Any | None = None,
    ):
        """Initialize skill service.

        Args:
            skill_registry: SkillRegistry for skill discovery
            skill_manager: SkillManager for skill operations
            skill_governance: SkillGovernance for approval workflow
            nexus_fs: NexusFS instance for filesystem operations
        """
        self._skill_registry = skill_registry
        self._skill_manager = skill_manager
        self._skill_governance = skill_governance
        self.nexus_fs = nexus_fs

        logger.info("[SkillService] Initialized")

    # =========================================================================
    # Public API: Skill Creation
    # =========================================================================

    @rpc_expose(description="Create a new skill from template")
    async def skills_create(
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
            name: Skill name (lowercase, hyphen-separated)
            description: Skill description
            template: Template name (basic/advanced/connector, default: "basic")
            tier: Target tier (agent/user/tenant/system, default: "user")
            author: Optional author name
            context: Operation context with user_id, tenant_id

        Returns:
            Dictionary containing:
                - skill_path: Path to created skill (str)
                - name: Skill name (str)
                - tier: Target tier (str)
                - template: Template used (str)

        Examples:
            # Create basic skill
            result = service.skills_create(
                name="data-analyzer",
                description="Analyze CSV data",
                tier="user"
            )
            print(f"Created at {result['skill_path']}")

            # Create from advanced template
            result = service.skills_create(
                name="api-client",
                description="REST API client",
                template="advanced",
                tier="tenant",
                author="Team Alpha"
            )
        """
        manager = self._get_skill_manager()

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

    @rpc_expose(description="Create a skill from web content")
    async def skills_create_from_content(
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
            content: Skill markdown content (SKILL.md contents)
            tier: Target tier (agent/user/tenant/system, default: "user")
            author: Optional author name
            source_url: Optional source URL for attribution
            metadata: Optional additional metadata
            context: Operation context

        Returns:
            Dictionary containing:
                - skill_path: Path to created skill (str)
                - name: Skill name (str)
                - tier: Target tier (str)
                - source_url: Source URL if provided (str|None)

        Examples:
            # Create skill from custom markdown
            content = \"\"\"
            # My Custom Skill

            This skill does X, Y, Z.

            ## Usage
            ...
            \"\"\"
            result = service.skills_create_from_content(
                name="custom-skill",
                description="Custom functionality",
                content=content,
                tier="user"
            )

            # Create with attribution
            result = service.skills_create_from_content(
                name="imported-skill",
                description="Imported from docs",
                content=scraped_content,
                source_url="https://example.com/docs",
                tier="agent"
            )
        """
        manager = self._get_skill_manager()

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

    @rpc_expose(description="Create skill from file or URL (auto-detects type)")
    async def skills_create_from_file(
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
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Create a skill from file or URL (auto-detects type).

        Supports:
        - PDFs (local files or base64 encoded)
        - Web URLs (scrapes content)
        - AI enhancement for better skill generation

        Args:
            source: File path or URL
            file_data: Base64 encoded file data (for remote calls)
            name: Skill name (auto-generated if not provided)
            description: Skill description
            tier: Target tier (agent/user/tenant/system, default: "agent")
            use_ai: Enable AI enhancement (default: False)
            use_ocr: Enable OCR for scanned PDFs (default: False)
            extract_tables: Extract tables from documents (default: False)
            extract_images: Extract images from documents (default: False)
            author: Optional author name
            context: Operation context

        Returns:
            Dictionary containing:
                - skill_path: Path to created skill (str)
                - name: Skill name (str)
                - tier: Target tier (str)
                - source: Source file/URL (str)

        Raises:
            RuntimeError: If skill-seekers plugin not installed
            ValueError: If unsupported source type

        Examples:
            # Create from PDF
            result = service.skills_create_from_file(
                source="/path/to/document.pdf",
                use_ai=True,
                extract_tables=True,
                tier="agent"
            )

            # Create from URL
            result = service.skills_create_from_file(
                source="https://docs.example.com/api",
                name="api-docs",
                description="API documentation",
                use_ai=True,
                tier="tenant"
            )

            # Create from base64 PDF (remote call)
            result = service.skills_create_from_file(
                source="document.pdf",
                file_data="base64_encoded_pdf_data...",
                use_ocr=True,
                tier="user"
            )

        Note:
            Requires nexus-plugin-skill-seekers to be installed.
        """
        import base64
        import tempfile
        from pathlib import Path
        from urllib.parse import urlparse

        # Load plugin
        try:
            from nexus_skill_seekers.plugin import SkillSeekersPlugin

            plugin = SkillSeekersPlugin(nexus_fs=self.nexus_fs)
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

    # =========================================================================
    # Public API: Skill Discovery and Management
    # =========================================================================

    @rpc_expose(description="List all skills")
    async def skills_list(
        self,
        tier: str | None = None,
        include_metadata: bool = True,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """List all skills accessible to the user.

        Args:
            tier: Filter by tier (agent/user/tenant/system, None for all)
            include_metadata: Include full metadata (default: True)
            context: Operation context for access control

        Returns:
            Dictionary containing:
                - skills: List of skill dicts (list[dict])
                - count: Total count (int)
                - tiers: Available tiers (list[str])

        Examples:
            # List all skills
            result = service.skills_list()
            for skill in result['skills']:
                print(f"{skill['name']}: {skill['description']}")

            # List user-tier skills only
            result = service.skills_list(tier="user", context=context)

            # List names only
            result = service.skills_list(include_metadata=False)
        """
        registry = self._get_skill_registry()

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

    @rpc_expose(description="Get detailed skill information")
    async def skills_info(
        self,
        skill_name: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Get detailed skill information.

        Args:
            skill_name: Name of the skill
            context: Operation context

        Returns:
            Dictionary containing:
                - name: Skill name (str)
                - description: Skill description (str)
                - version: Skill version (str)
                - author: Author name (str|None)
                - tier: Skill tier (str)
                - file_path: Skill file path (str)
                - requires: Dependencies list (list[str])
                - created_at: Creation timestamp (str)
                - modified_at: Modification timestamp (str)
                - resolved_dependencies: Resolved dependency tree (dict)

        Examples:
            # Get skill info
            info = service.skills_info(
                skill_name="data-analyzer",
                context=context
            )
            print(f"Author: {info['author']}")
            print(f"Created: {info['created_at']}")
        """
        registry = self._get_skill_registry()

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

    @rpc_expose(description="Search skills by description")
    async def skills_search(
        self,
        query: str,
        tier: str | None = None,
        limit: int = 10,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Search skills by description or content.

        Args:
            query: Search query string
            tier: Filter by tier (optional)
            limit: Maximum results (default: 10)
            context: Operation context

        Returns:
            Dictionary containing:
                - skills: List of matching skill dicts (list[dict])
                - count: Result count (int)
                - query: Original query (str)

        Examples:
            # Search skills
            results = service.skills_search(
                query="data analysis",
                context=context
            )
            for skill in results['skills']:
                print(f"Found: {skill['name']}")

            # Search in specific tier
            results = service.skills_search(
                query="API",
                tier="tenant",
                context=context
            )
        """
        manager = self._get_skill_manager()

        results = await manager.search_skills(query=query, tier=tier, limit=limit)
        # Convert to serializable format
        results_data = [{"skill_name": name, "score": score} for name, score in results]
        return {"results": results_data, "query": query, "count": len(results_data)}

    # =========================================================================
    # Public API: Skill Publishing and Forking
    # =========================================================================

    @rpc_expose(description="Fork an existing skill")
    async def skills_fork(
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
            Dictionary containing:
                - forked_path: Path to forked skill (str)
                - source_name: Source skill name (str)
                - target_name: Target skill name (str)
                - tier: Target tier (str)

        Examples:
            # Fork a skill
            result = service.skills_fork(
                source_name="api-client",
                target_name="my-api-client",
                tier="user",
                context=context
            )
            print(f"Forked to {result['forked_path']}")
        """
        manager = self._get_skill_manager()
        registry = self._get_skill_registry()

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

    @rpc_expose(description="Publish skill to another tier")
    async def skills_publish(
        self,
        skill_name: str,
        source_tier: str = "agent",
        target_tier: str = "tenant",
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Publish skill to another tier.

        Args:
            skill_name: Skill name
            source_tier: Source tier (default: "agent")
            target_tier: Target tier (default: "tenant")
            context: Operation context

        Returns:
            Dictionary containing:
                - published_path: Path to published skill (str)
                - skill_name: Skill name (str)
                - source_tier: Source tier (str)
                - target_tier: Target tier (str)

        Examples:
            # Publish to tenant tier
            result = service.skills_publish(
                skill_path="/skills/user/my-skill",
                target_tier="tenant",
                context=context
            )
        """
        manager = self._get_skill_manager()

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

    # =========================================================================
    # Public API: Package Import/Export
    # =========================================================================

    @rpc_expose(description="Import skill from .zip/.skill package")
    async def skills_import(
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
        importer = SkillImporter(cast(NexusFilesystem, self.nexus_fs), registry)

        # Import skill
        return await importer.import_from_zip(
            zip_data=zip_bytes,
            tier=tier,
            allow_overwrite=allow_overwrite,
            context=context,
        )

    @rpc_expose(description="Validate skill ZIP package without importing")
    async def skills_validate_zip(
        self,
        package_path: str,
    ) -> dict[str, Any]:
        """Validate skill package without importing.

        Args:
            package_path: Path to .skill/.zip package file

        Returns:
            Dictionary containing:
                - valid: Whether package is valid (bool)
                - errors: List of validation errors (list[str])
                - warnings: List of warnings (list[str])
                - metadata: Extracted metadata if valid (dict|None)

        Examples:
            # Validate before import
            validation = service.skills_validate_zip(
                package_path="/downloads/skill.skill"
            )
            if validation['valid']:
                print("Package is valid")
                # Now import
            else:
                print(f"Errors: {validation['errors']}")
        """
        import base64

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills.importer import SkillImporter

        # Update signature to match source
        zip_data = package_path  # For now, accept as-is
        zip_bytes = base64.b64decode(zip_data)

        from typing import cast

        registry = self._get_skill_registry()
        importer = SkillImporter(cast(NexusFilesystem, self.nexus_fs), registry)

        return await importer.validate_zip(zip_bytes)

    @rpc_expose(description="Export skill to .skill package")
    async def skills_export(
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
            format: Export format (default: "generic")
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
            from nexus.core.exceptions import ValidationError

            raise ValidationError(f"Failed to export skill '{skill_name}'")

        # Encode to base64
        zip_base64 = base64.b64encode(zip_bytes).decode("utf-8")

        return {
            "skill_name": skill_name,
            "zip_data": zip_base64,
            "size_bytes": len(zip_bytes),
            "filename": f"{skill_name}.skill",  # Suggested filename with .skill extension (ZIP format)
        }

    # =========================================================================
    # Public API: Approval Workflow
    # =========================================================================

    @rpc_expose(description="Submit skill for approval")
    async def skills_submit_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: builtins.list[str] | None = None,
        comments: str | None = None,
        _context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Submit a skill for approval.

        Args:
            skill_name: Skill name
            submitted_by: Submitter ID
            reviewers: Optional list of reviewer IDs
            comments: Optional submission comments
            context: Operation context

        Returns:
            Dictionary containing:
                - approval_id: Approval request ID (str)
                - skill_name: Skill name (str)
                - submitted_by: Submitter ID (str)
                - reviewers: List of reviewer IDs (list[str]|None)
        """
        governance = self._get_skill_governance()

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

    @rpc_expose(description="Approve a skill")
    async def skills_approve(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        tenant_id: str | None = None,
        _context: OperationContext | None = None,
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
            Dictionary containing:
                - approval_id: Approval request ID (str)
                - reviewed_by: Reviewer ID (str)
                - reviewer_type: Reviewer type (str)
                - status: Status "approved" (str)
        """
        governance = self._get_skill_governance()

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

    @rpc_expose(description="Reject a skill")
    async def skills_reject(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        tenant_id: str | None = None,
        _context: OperationContext | None = None,
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
            Dictionary containing:
                - approval_id: Approval request ID (str)
                - reviewed_by: Reviewer ID (str)
                - reviewer_type: Reviewer type (str)
                - status: Status "rejected" (str)
        """
        governance = self._get_skill_governance()

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

    @rpc_expose(description="List approval requests")
    async def skills_list_approvals(
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
            Dictionary containing:
                - approvals: List of approval dicts (list[dict])
                - count: Number of approvals (int)
        """
        governance = self._get_skill_governance()

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

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_skill_registry(self) -> Any:
        """Get or create SkillRegistry instance.

        Returns:
            SkillRegistry instance
        """
        from typing import cast

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills import SkillRegistry

        if self._skill_registry is not None:
            return self._skill_registry

        if self.nexus_fs is None:
            raise RuntimeError("NexusFS not configured for SkillService")

        return SkillRegistry(cast(NexusFilesystem, self.nexus_fs))

    def _get_skill_manager(self) -> Any:
        """Get or create SkillManager instance.

        Returns:
            SkillManager instance
        """
        from typing import cast

        from nexus.core.nexus_fs import NexusFilesystem
        from nexus.skills import SkillManager

        if self._skill_manager is not None:
            return self._skill_manager

        if self.nexus_fs is None:
            raise RuntimeError("NexusFS not configured for SkillService")

        registry = self._get_skill_registry()
        return SkillManager(cast(NexusFilesystem, self.nexus_fs), registry)

    def _get_skill_governance(self) -> Any:
        """Get or create SkillGovernance instance.

        Returns:
            SkillGovernance instance
        """
        from nexus.skills import SkillGovernance

        if self._skill_governance is not None:
            return self._skill_governance

        # Get database connection if available
        db_conn = None
        if (
            self.nexus_fs
            and hasattr(self.nexus_fs, "metadata_store")
            and self.nexus_fs.metadata_store
            and hasattr(self.nexus_fs.metadata_store, "session")
        ):
            from nexus.cli.commands.skills import SQLAlchemyDatabaseConnection

            db_conn = SQLAlchemyDatabaseConnection(self.nexus_fs.metadata_store.session)

        return SkillGovernance(db_connection=db_conn)


# =============================================================================
# Phase 2 Extraction Progress
# =============================================================================
#
# Status: Skeleton created ✅
#
# TODO (in order of priority):
# 1. [ ] Extract skills_create() - Create from template
# 2. [ ] Extract skills_create_from_content() - Create from markdown
# 3. [ ] Extract skills_create_from_file() - Create from file/URL
# 4. [ ] Extract skills_list() - List all skills with tier filtering
# 5. [ ] Extract skills_info() - Get detailed skill metadata
# 6. [ ] Extract skills_search() - Search by description/content
# 7. [ ] Extract skills_fork() - Fork existing skills
# 8. [ ] Extract skills_publish() - Publish to higher tier
# 9. [ ] Extract skills_import() - Import from package
# 10. [ ] Extract skills_validate_zip() - Validate package format
# 11. [ ] Extract skills_export() - Export as package
# 12. [ ] Extract skills_submit_approval() - Submit for approval
# 13. [ ] Extract skills_approve() - Approve submission
# 14. [ ] Extract skills_reject() - Reject submission
# 15. [ ] Extract skills_list_approvals() - List approval requests
# 16. [ ] Extract helper methods (registry, manager, governance getters)
# 17. [ ] Add unit tests for SkillService
# 18. [ ] Update NexusFS to use composition
# 19. [ ] Add backward compatibility shims with deprecation warnings
# 20. [ ] Update documentation and migration guide
#
# Lines extracted: 0 / 874 (0%)
# Files affected: 1 created, 0 modified
#
# This is a phased extraction to maintain working code at each step.
#
