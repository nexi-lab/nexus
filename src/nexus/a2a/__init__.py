"""Google A2A (Agent-to-Agent) protocol endpoint for Nexus.

This module implements the A2A protocol specification, enabling Nexus to
participate in the agent interoperability ecosystem as one of three protocol
surfaces (alongside VFS and MCP).

See: https://a2a-protocol.org/latest/specification/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import APIRouter


def create_a2a_router(
    *,
    nexus_fs: Any = None,
    config: Any = None,
    base_url: str | None = None,
    auth_required: bool = False,
    data_dir: str | None = None,
) -> APIRouter:
    """Create the A2A protocol FastAPI router.

    Args:
        nexus_fs: NexusFS instance for backend operations.
        config: NexusConfig instance for Agent Card generation.
        base_url: Base URL for the Agent Card endpoint URL field.
            If None, defaults to "http://localhost:2026".
        auth_required: When True, all A2A operational endpoints
            require a valid Authorization header.
        data_dir: Server data directory.  When provided, A2A tasks
            are persisted as JSON files under ``{data_dir}/a2a/tasks/``.
            When None, tasks are stored in-memory only.

    Returns:
        Configured FastAPI APIRouter with A2A endpoints.
    """
    from nexus.a2a.router import build_router
    from nexus.a2a.task_manager import TaskManager

    task_manager: TaskManager | None = None
    if data_dir is not None:
        from nexus.a2a.stores.local_driver import LocalStorageDriver
        from nexus.a2a.stores.vfs import VFSTaskStore

        storage = LocalStorageDriver(root=data_dir)
        store = VFSTaskStore(storage=storage)
        task_manager = TaskManager(store=store)

    return build_router(
        _nexus_fs=nexus_fs,
        config=config,
        base_url=base_url,
        task_manager=task_manager,
        auth_required=auth_required,
    )
