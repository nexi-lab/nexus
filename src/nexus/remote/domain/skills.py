"""Skills management domain client (sync + async).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

import builtins
from typing import Any


class SkillsClient:
    """Skills management domain client (sync)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    def create(
        self,
        name: str,
        description: str,
        template: str = "basic",
        tier: str = "agent",
        author: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "description": description,
            "template": template,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        return self._call_rpc("skills_create", params)  # type: ignore[no-any-return]

    def create_from_content(
        self,
        name: str,
        description: str,
        content: str,
        tier: str = "agent",
        author: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "description": description,
            "content": content,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        if source_url is not None:
            params["source_url"] = source_url
        if metadata is not None:
            params["metadata"] = metadata
        return self._call_rpc("skills_create_from_content", params)  # type: ignore[no-any-return]

    def create_from_file(
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
        author: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "source": source,
            "tier": tier,
            "use_ai": use_ai,
            "use_ocr": use_ocr,
            "extract_tables": extract_tables,
            "extract_images": extract_images,
        }
        if file_data is not None:
            params["file_data"] = file_data
        if name is not None:
            params["name"] = name
        if description is not None:
            params["description"] = description
        if author is not None:
            params["_author"] = author
        return self._call_rpc("skills_create_from_file", params)  # type: ignore[no-any-return]

    def list(
        self,
        tier: str | None = None,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_metadata": include_metadata}
        if tier is not None:
            params["tier"] = tier
        return self._call_rpc("skills_list", params)  # type: ignore[no-any-return]

    def info(self, skill_name: str) -> dict[str, Any]:
        return self._call_rpc("skills_info", {"skill_name": skill_name})  # type: ignore[no-any-return]

    def fork(
        self,
        source_name: str,
        target_name: str,
        tier: str = "agent",
        author: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "source_name": source_name,
            "target_name": target_name,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        return self._call_rpc("skills_fork", params)  # type: ignore[no-any-return]

    def publish(
        self,
        skill_name: str,
        source_tier: str = "agent",
        target_tier: str = "zone",
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "skills_publish",
            {
                "skill_name": skill_name,
                "source_tier": source_tier,
                "target_tier": target_tier,
            },
        )

    def search(
        self,
        query: str,
        tier: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if tier is not None:
            params["tier"] = tier
        return self._call_rpc("skills_search", params)  # type: ignore[no-any-return]

    def submit_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: builtins.list[str] | None = None,
        comments: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "skill_name": skill_name,
            "submitted_by": submitted_by,
        }
        if reviewers is not None:
            params["reviewers"] = reviewers
        if comments is not None:
            params["comments"] = comments
        return self._call_rpc("skills_submit_approval", params)  # type: ignore[no-any-return]

    def approve(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "reviewed_by": reviewed_by,
            "reviewer_type": reviewer_type,
        }
        if comments is not None:
            params["comments"] = comments
        if zone_id is not None:
            params["zone_id"] = zone_id
        return self._call_rpc("skills_approve", params)  # type: ignore[no-any-return]

    def reject(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "reviewed_by": reviewed_by,
            "reviewer_type": reviewer_type,
        }
        if comments is not None:
            params["comments"] = comments
        if zone_id is not None:
            params["zone_id"] = zone_id
        return self._call_rpc("skills_reject", params)  # type: ignore[no-any-return]

    def list_approvals(
        self,
        status: str | None = None,
        skill_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if skill_name is not None:
            params["skill_name"] = skill_name
        return self._call_rpc("skills_list_approvals", params)  # type: ignore[no-any-return]

    def import_zip(
        self,
        zip_data: str,
        tier: str = "user",
        allow_overwrite: bool = False,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "skills_import",
            {"zip_data": zip_data, "tier": tier, "allow_overwrite": allow_overwrite},
        )

    def validate_zip(self, zip_data: str) -> dict[str, Any]:
        return self._call_rpc("skills_validate_zip", {"zip_data": zip_data})  # type: ignore[no-any-return]

    def export(
        self,
        skill_name: str,
        format: str = "generic",
        include_dependencies: bool = False,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "skills_export",
            {
                "skill_name": skill_name,
                "format": format,
                "include_dependencies": include_dependencies,
            },
        )

    def share(self, skill_path: str, share_with: str) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "skills_share", {"skill_path": skill_path, "share_with": share_with}
        )

    def unshare(self, skill_path: str, unshare_from: str) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "skills_unshare", {"skill_path": skill_path, "unshare_from": unshare_from}
        )

    def discover(self, filter: str = "all") -> dict[str, Any]:
        return self._call_rpc("skills_discover", {"filter": filter})  # type: ignore[no-any-return]

    def subscribe(self, skill_path: str) -> dict[str, Any]:
        return self._call_rpc("skills_subscribe", {"skill_path": skill_path})  # type: ignore[no-any-return]

    def unsubscribe(self, skill_path: str) -> dict[str, Any]:
        return self._call_rpc("skills_unsubscribe", {"skill_path": skill_path})  # type: ignore[no-any-return]

    def get_prompt_context(self, max_skills: int = 50) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "skills_get_prompt_context", {"max_skills": max_skills}
        )

    def load(self, skill_path: str) -> dict[str, Any]:
        return self._call_rpc("skills_load", {"skill_path": skill_path})  # type: ignore[no-any-return]


class AsyncSkillsClient:
    """Skills management domain client (async)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    async def create(
        self,
        name: str,
        description: str,
        template: str = "basic",
        tier: str = "agent",
        author: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "description": description,
            "template": template,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        return await self._call_rpc("skills_create", params)  # type: ignore[no-any-return]

    async def create_from_content(
        self,
        name: str,
        description: str,
        content: str,
        tier: str = "agent",
        author: str | None = None,
        source_url: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "name": name,
            "description": description,
            "content": content,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        if source_url is not None:
            params["source_url"] = source_url
        if metadata is not None:
            params["metadata"] = metadata
        return await self._call_rpc("skills_create_from_content", params)  # type: ignore[no-any-return]

    async def create_from_file(
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
        author: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "source": source,
            "tier": tier,
            "use_ai": use_ai,
            "use_ocr": use_ocr,
            "extract_tables": extract_tables,
            "extract_images": extract_images,
        }
        if file_data is not None:
            params["file_data"] = file_data
        if name is not None:
            params["name"] = name
        if description is not None:
            params["description"] = description
        if author is not None:
            params["_author"] = author
        return await self._call_rpc("skills_create_from_file", params)  # type: ignore[no-any-return]

    async def list(
        self,
        tier: str | None = None,
        include_metadata: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"include_metadata": include_metadata}
        if tier is not None:
            params["tier"] = tier
        return await self._call_rpc("skills_list", params)  # type: ignore[no-any-return]

    async def info(self, skill_name: str) -> dict[str, Any]:
        return await self._call_rpc("skills_info", {"skill_name": skill_name})  # type: ignore[no-any-return]

    async def fork(
        self,
        source_name: str,
        target_name: str,
        tier: str = "agent",
        author: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "source_name": source_name,
            "target_name": target_name,
            "tier": tier,
        }
        if author is not None:
            params["author"] = author
        return await self._call_rpc("skills_fork", params)  # type: ignore[no-any-return]

    async def publish(
        self,
        skill_name: str,
        source_tier: str = "agent",
        target_tier: str = "zone",
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_publish",
            {
                "skill_name": skill_name,
                "source_tier": source_tier,
                "target_tier": target_tier,
            },
        )

    async def search(
        self,
        query: str,
        tier: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if tier is not None:
            params["tier"] = tier
        return await self._call_rpc("skills_search", params)  # type: ignore[no-any-return]

    async def submit_approval(
        self,
        skill_name: str,
        submitted_by: str,
        reviewers: builtins.list[str] | None = None,
        comments: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "skill_name": skill_name,
            "submitted_by": submitted_by,
        }
        if reviewers is not None:
            params["reviewers"] = reviewers
        if comments is not None:
            params["comments"] = comments
        return await self._call_rpc("skills_submit_approval", params)  # type: ignore[no-any-return]

    async def approve(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "reviewed_by": reviewed_by,
            "reviewer_type": reviewer_type,
        }
        if comments is not None:
            params["comments"] = comments
        if zone_id is not None:
            params["zone_id"] = zone_id
        return await self._call_rpc("skills_approve", params)  # type: ignore[no-any-return]

    async def reject(
        self,
        approval_id: str,
        reviewed_by: str,
        reviewer_type: str = "user",
        comments: str | None = None,
        zone_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "approval_id": approval_id,
            "reviewed_by": reviewed_by,
            "reviewer_type": reviewer_type,
        }
        if comments is not None:
            params["comments"] = comments
        if zone_id is not None:
            params["zone_id"] = zone_id
        return await self._call_rpc("skills_reject", params)  # type: ignore[no-any-return]

    async def list_approvals(
        self,
        status: str | None = None,
        skill_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if status is not None:
            params["status"] = status
        if skill_name is not None:
            params["skill_name"] = skill_name
        return await self._call_rpc("skills_list_approvals", params)  # type: ignore[no-any-return]

    async def import_zip(
        self,
        zip_data: str,
        tier: str = "user",
        allow_overwrite: bool = False,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_import",
            {"zip_data": zip_data, "tier": tier, "allow_overwrite": allow_overwrite},
        )

    async def validate_zip(self, zip_data: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_validate_zip", {"zip_data": zip_data}
        )

    async def export(
        self,
        skill_name: str,
        format: str = "generic",
        include_dependencies: bool = False,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_export",
            {
                "skill_name": skill_name,
                "format": format,
                "include_dependencies": include_dependencies,
            },
        )

    async def share(self, skill_path: str, share_with: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_share", {"skill_path": skill_path, "share_with": share_with}
        )

    async def unshare(self, skill_path: str, unshare_from: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_unshare", {"skill_path": skill_path, "unshare_from": unshare_from}
        )

    async def discover(self, filter: str = "all") -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_discover", {"filter": filter}
        )

    async def subscribe(self, skill_path: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_subscribe", {"skill_path": skill_path}
        )

    async def unsubscribe(self, skill_path: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_unsubscribe", {"skill_path": skill_path}
        )

    async def get_prompt_context(self, max_skills: int = 50) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_get_prompt_context", {"max_skills": max_skills}
        )

    async def load(self, skill_path: str) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "skills_load", {"skill_path": skill_path}
        )
