"""Memory management domain client (sync + async).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

import builtins
from typing import Any


class MemoryClient:
    """Memory management domain client (sync).

    Provides the same interface as core.memory_api.Memory but makes RPC calls
    to a remote Nexus server instead of direct database access.
    """

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    # --- Trajectory Methods ---

    def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        _parent_trajectory_id: str | None = None,
        _metadata: dict[str, Any] | None = None,
        _path: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        result = self._call_rpc("start_trajectory", params)
        return result["trajectory_id"]  # type: ignore[no-any-return]

    def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        _metadata: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "step_type": step_type,
            "description": description,
        }
        if result is not None:
            params["result"] = result
        self._call_rpc("log_trajectory_step", params)

    def log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.log_step(trajectory_id, step_type, description, result, metadata)

    def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        _metrics: dict[str, Any] | None = None,
    ) -> str:
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        result = self._call_rpc("complete_trajectory", params)
        return result["trajectory_id"]  # type: ignore[no-any-return]

    def query_trajectories(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        if limit != 50:
            params["limit"] = limit
        result = self._call_rpc("query_trajectories", params)
        return result.get("trajectories", [])  # type: ignore[no-any-return]

    # --- Playbook Methods ---

    def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        return self._call_rpc("get_playbook", {"playbook_name": playbook_name})  # type: ignore[no-any-return]

    def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if scope is not None:
            params["scope"] = scope
        if limit != 50:
            params["limit"] = limit
        result = self._call_rpc("query_playbooks", params)
        return result.get("playbooks", [])  # type: ignore[no-any-return]

    def process_relearning(self, limit: int = 10) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if limit != 10:
            params["limit"] = limit
        result = self._call_rpc("process_relearning", params)
        return result.get("results", [])  # type: ignore[no-any-return]

    def curate_playbook(
        self,
        reflection_memory_ids: builtins.list[str],
        playbook_name: str = "default",
        merge_threshold: float = 0.7,
    ) -> dict[str, Any]:
        return self._call_rpc(  # type: ignore[no-any-return]
            "curate_playbook",
            {
                "reflection_memory_ids": reflection_memory_ids,
                "playbook_name": playbook_name,
                "merge_threshold": merge_threshold,
            },
        )

    def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"min_trajectories": min_trajectories}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if since is not None:
            params["since"] = since
        if task_type is not None:
            params["task_type"] = task_type
        return self._call_rpc("batch_reflect", params)  # type: ignore[no-any-return]

    # --- Memory Storage Methods ---

    def store(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "agent",
        importance: float = 0.5,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        tags: builtins.list[str] | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "content": content,
            "memory_type": memory_type,
            "scope": scope,
            "importance": importance,
        }
        if namespace is not None:
            params["namespace"] = namespace
        if path_key is not None:
            params["path_key"] = path_key
        if state != "active":
            params["state"] = state
        if tags is not None:
            params["tags"] = tags
        result = self._call_rpc("store_memory", params)
        return result["memory_id"]  # type: ignore[no-any-return]

    def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        if namespace is not None:
            params["namespace"] = namespace
        if namespace_prefix is not None:
            params["namespace_prefix"] = namespace_prefix
        if memory_type is not None:
            params["memory_type"] = memory_type
        if state is not None:
            params["state"] = state
        result = self._call_rpc("list_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        else:
            if namespace is not None:
                params["namespace"] = namespace
            if path_key is not None:
                params["path_key"] = path_key
        result = self._call_rpc("retrieve_memory", params)
        return result.get("memory")  # type: ignore[no-any-return]

    def query(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        state: str | None = "active",
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        if state is not None:
            params["state"] = state
        result = self._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        search_mode: str = "hybrid",
        embedding_provider: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        if search_mode != "hybrid":
            params["search_mode"] = search_mode
        if embedding_provider is not None:
            if hasattr(embedding_provider, "__class__"):
                provider_name = embedding_provider.__class__.__name__.lower()
                if "openrouter" in provider_name:
                    params["embedding_provider"] = "openrouter"
                elif "openai" in provider_name:
                    params["embedding_provider"] = "openai"
                elif "voyage" in provider_name:
                    params["embedding_provider"] = "voyage"
            elif isinstance(embedding_provider, str):
                params["embedding_provider"] = embedding_provider
        result = self._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    def delete(self, memory_id: str) -> bool:
        result = self._call_rpc("delete_memory", {"memory_id": memory_id})
        return result["deleted"]  # type: ignore[no-any-return]

    def approve(self, memory_id: str) -> bool:
        result = self._call_rpc("approve_memory", {"memory_id": memory_id})
        return result["approved"]  # type: ignore[no-any-return]

    def deactivate(self, memory_id: str) -> bool:
        result = self._call_rpc("deactivate_memory", {"memory_id": memory_id})
        return result["deactivated"]  # type: ignore[no-any-return]

    def approve_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return self._call_rpc("approve_memory_batch", {"memory_ids": memory_ids})  # type: ignore[no-any-return]

    def deactivate_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return self._call_rpc("deactivate_memory_batch", {"memory_ids": memory_ids})  # type: ignore[no-any-return]

    def delete_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return self._call_rpc("delete_memory_batch", {"memory_ids": memory_ids})  # type: ignore[no-any-return]


class AsyncMemoryClient:
    """Memory management domain client (async)."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    # --- Trajectory Methods ---

    async def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
        _parent_trajectory_id: str | None = None,
        _metadata: dict[str, Any] | None = None,
        _path: str | None = None,
    ) -> str:
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        result = await self._call_rpc("start_trajectory", params)
        return result["trajectory_id"]  # type: ignore[no-any-return]

    async def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        _metadata: dict[str, Any] | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "step_type": step_type,
            "description": description,
        }
        if result is not None:
            params["result"] = result
        await self._call_rpc("log_trajectory_step", params)

    async def log_trajectory_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self.log_step(trajectory_id, step_type, description, result, metadata)

    async def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
        _metrics: dict[str, Any] | None = None,
    ) -> str:
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        result = await self._call_rpc("complete_trajectory", params)
        return result["trajectory_id"]  # type: ignore[no-any-return]

    async def query_trajectories(
        self,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if status is not None:
            params["status"] = status
        if limit != 50:
            params["limit"] = limit
        result = await self._call_rpc("query_trajectories", params)
        return result.get("trajectories", [])  # type: ignore[no-any-return]

    # --- Playbook Methods ---

    async def get_playbook(self, playbook_name: str = "default") -> dict[str, Any] | None:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "get_playbook", {"playbook_name": playbook_name}
        )

    async def query_playbooks(
        self,
        agent_id: str | None = None,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if scope is not None:
            params["scope"] = scope
        if limit != 50:
            params["limit"] = limit
        result = await self._call_rpc("query_playbooks", params)
        return result.get("playbooks", [])  # type: ignore[no-any-return]

    async def process_relearning(self, limit: int = 10) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if limit != 10:
            params["limit"] = limit
        result = await self._call_rpc("process_relearning", params)
        return result.get("results", [])  # type: ignore[no-any-return]

    async def curate_playbook(
        self,
        reflection_memory_ids: builtins.list[str],
        playbook_name: str = "default",
        merge_threshold: float = 0.7,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "curate_playbook",
            {
                "reflection_memory_ids": reflection_memory_ids,
                "playbook_name": playbook_name,
                "merge_threshold": merge_threshold,
            },
        )

    async def batch_reflect(
        self,
        agent_id: str | None = None,
        since: str | None = None,
        min_trajectories: int = 10,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"min_trajectories": min_trajectories}
        if agent_id is not None:
            params["agent_id"] = agent_id
        if since is not None:
            params["since"] = since
        if task_type is not None:
            params["task_type"] = task_type
        return await self._call_rpc("batch_reflect", params)  # type: ignore[no-any-return]

    # --- Memory Storage Methods ---

    async def store(
        self,
        content: str,
        memory_type: str = "fact",
        scope: str = "agent",
        importance: float = 0.5,
        namespace: str | None = None,
        path_key: str | None = None,
        state: str = "active",
        tags: builtins.list[str] | None = None,
    ) -> str:
        params: dict[str, Any] = {
            "content": content,
            "memory_type": memory_type,
            "scope": scope,
            "importance": importance,
        }
        if namespace is not None:
            params["namespace"] = namespace
        if path_key is not None:
            params["path_key"] = path_key
        if state != "active":
            params["state"] = state
        if tags is not None:
            params["tags"] = tags
        result = await self._call_rpc("store_memory", params)
        return result["memory_id"]  # type: ignore[no-any-return]

    async def list(
        self,
        scope: str | None = None,
        memory_type: str | None = None,
        namespace: str | None = None,
        namespace_prefix: str | None = None,
        state: str | None = "active",
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        if namespace is not None:
            params["namespace"] = namespace
        if namespace_prefix is not None:
            params["namespace_prefix"] = namespace_prefix
        if memory_type is not None:
            params["memory_type"] = memory_type
        if state is not None:
            params["state"] = state
        result = await self._call_rpc("list_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    async def retrieve(
        self,
        namespace: str | None = None,
        path_key: str | None = None,
        path: str | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {}
        if path is not None:
            params["path"] = path
        else:
            if namespace is not None:
                params["namespace"] = namespace
            if path_key is not None:
                params["path_key"] = path_key
        result = await self._call_rpc("retrieve_memory", params)
        return result.get("memory")  # type: ignore[no-any-return]

    async def query(
        self,
        memory_type: str | None = None,
        scope: str | None = None,
        state: str | None = "active",
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        if state is not None:
            params["state"] = state
        result = await self._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    async def search(
        self,
        query: str,
        scope: str | None = None,
        memory_type: str | None = None,
        limit: int = 10,
        search_mode: str = "hybrid",
        embedding_provider: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"query": query, "limit": limit}
        if memory_type is not None:
            params["memory_type"] = memory_type
        if scope is not None:
            params["scope"] = scope
        if search_mode != "hybrid":
            params["search_mode"] = search_mode
        if embedding_provider is not None:
            if hasattr(embedding_provider, "__class__"):
                provider_name = embedding_provider.__class__.__name__.lower()
                if "openrouter" in provider_name:
                    params["embedding_provider"] = "openrouter"
                elif "openai" in provider_name:
                    params["embedding_provider"] = "openai"
                elif "voyage" in provider_name:
                    params["embedding_provider"] = "voyage"
            elif isinstance(embedding_provider, str):
                params["embedding_provider"] = embedding_provider
        result = await self._call_rpc("query_memories", params)
        return result["memories"]  # type: ignore[no-any-return]

    async def delete(self, memory_id: str) -> bool:
        result = await self._call_rpc("delete_memory", {"memory_id": memory_id})
        return result["deleted"]  # type: ignore[no-any-return]

    async def approve(self, memory_id: str) -> bool:
        result = await self._call_rpc("approve_memory", {"memory_id": memory_id})
        return result["approved"]  # type: ignore[no-any-return]

    async def deactivate(self, memory_id: str) -> bool:
        result = await self._call_rpc("deactivate_memory", {"memory_id": memory_id})
        return result["deactivated"]  # type: ignore[no-any-return]

    async def approve_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "approve_memory_batch", {"memory_ids": memory_ids}
        )

    async def deactivate_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "deactivate_memory_batch", {"memory_ids": memory_ids}
        )

    async def delete_batch(self, memory_ids: builtins.list[str]) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "delete_memory_batch", {"memory_ids": memory_ids}
        )
