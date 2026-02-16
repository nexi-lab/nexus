"""ACE (Adaptive Concurrency Engine) domain client (async-only).

Issue #1603: Decompose remote/client.py into domain clients.
"""

from __future__ import annotations

import builtins
from typing import Any


class AsyncACEClient:
    """Async ACE API client."""

    def __init__(self, call_rpc: Any) -> None:
        self._call_rpc = call_rpc

    async def start_trajectory(
        self,
        task_description: str,
        task_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"task_description": task_description}
        if task_type is not None:
            params["task_type"] = task_type
        return await self._call_rpc("ace_start_trajectory", params)  # type: ignore[no-any-return]

    async def log_step(
        self,
        trajectory_id: str,
        step_type: str,
        description: str,
        result: Any = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "step_type": step_type,
            "description": description,
        }
        if result is not None:
            params["result"] = result
        return await self._call_rpc("ace_log_trajectory_step", params)  # type: ignore[no-any-return]

    async def complete_trajectory(
        self,
        trajectory_id: str,
        status: str,
        success_score: float | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"trajectory_id": trajectory_id, "status": status}
        if success_score is not None:
            params["success_score"] = success_score
        if error_message is not None:
            params["error_message"] = error_message
        return await self._call_rpc("ace_complete_trajectory", params)  # type: ignore[no-any-return]

    async def add_feedback(
        self,
        trajectory_id: str,
        feedback_type: str,
        score: float | None = None,
        source: str | None = None,
        message: str | None = None,
        metrics: dict | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "trajectory_id": trajectory_id,
            "feedback_type": feedback_type,
        }
        if score is not None:
            params["score"] = score
        if source is not None:
            params["source"] = source
        if message is not None:
            params["message"] = message
        if metrics is not None:
            params["metrics"] = metrics
        return await self._call_rpc("ace_add_feedback", params)  # type: ignore[no-any-return]

    async def get_trajectory_feedback(
        self,
        trajectory_id: str,
    ) -> builtins.list[dict[str, Any]]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "ace_get_trajectory_feedback", {"trajectory_id": trajectory_id}
        )

    async def get_effective_score(
        self,
        trajectory_id: str,
        strategy: str = "latest",
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "ace_get_effective_score",
            {"trajectory_id": trajectory_id, "strategy": strategy},
        )

    async def mark_for_relearning(
        self,
        trajectory_id: str,
        reason: str,
        priority: int = 5,
    ) -> dict[str, Any]:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "ace_mark_for_relearning",
            {"trajectory_id": trajectory_id, "reason": reason, "priority": priority},
        )

    async def query_trajectories(
        self,
        task_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if task_type is not None:
            params["task_type"] = task_type
        if status is not None:
            params["status"] = status
        return await self._call_rpc("ace_query_trajectories", params)  # type: ignore[no-any-return]

    async def create_playbook(
        self,
        name: str,
        description: str | None = None,
        scope: str = "agent",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "scope": scope}
        if description is not None:
            params["description"] = description
        return await self._call_rpc("ace_create_playbook", params)  # type: ignore[no-any-return]

    async def get_playbook(self, playbook_id: str) -> dict[str, Any] | None:
        return await self._call_rpc(  # type: ignore[no-any-return]
            "ace_get_playbook", {"playbook_id": playbook_id}
        )

    async def query_playbooks(
        self,
        scope: str | None = None,
        limit: int = 50,
    ) -> builtins.list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if scope is not None:
            params["scope"] = scope
        return await self._call_rpc("ace_query_playbooks", params)  # type: ignore[no-any-return]
