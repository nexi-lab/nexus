"""Memory API RPC handler functions.

Extracted from fastapi_server.py (#1602). All handlers accept ``nexus_fs``
as an explicit parameter.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def _get_memory_api_with_context(nexus_fs: NexusFS, context: Any) -> Any:
    """Get Memory API instance with authenticated context.

    Args:
        nexus_fs: NexusFS instance
        context: Operation context with zone/user/agent info

    Returns:
        Memory API instance with user/agent/zone from context
    """
    context_dict: dict[str, Any] = {}
    if context:
        if hasattr(context, "zone_id") and context.zone_id:
            context_dict["zone_id"] = context.zone_id
        if hasattr(context, "user_id") and context.user_id:
            context_dict["user_id"] = context.user_id
        elif hasattr(context, "user") and context.user:
            context_dict["user_id"] = context.user
        if hasattr(context, "agent_id") and context.agent_id:
            context_dict["agent_id"] = context.agent_id

    return nexus_fs._get_memory_api(context_dict if context_dict else None)


def handle_store_memory(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle store_memory RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    memory_id = memory_api.store(
        content=params.content,
        memory_type=params.memory_type,
        scope=params.scope,
        importance=params.importance,
        namespace=params.namespace,
        path_key=params.path_key,
        state=params.state,
    )
    return {"memory_id": memory_id}


def handle_list_memories(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle list_memories RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    memories = memory_api.list(
        scope=params.scope,
        memory_type=params.memory_type,
        namespace=params.namespace,
        namespace_prefix=params.namespace_prefix,
        state=params.state,
        limit=params.limit,
    )
    return {"memories": memories}


def handle_query_memories(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle query_memories RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)

    if params.query:
        embedding_provider_obj = None
        if params.embedding_provider:
            try:
                from nexus.search.embeddings import create_embedding_provider

                embedding_provider_obj = create_embedding_provider(
                    provider=params.embedding_provider
                )
            except Exception:
                pass

        search_mode = params.search_mode or "hybrid"
        memories = memory_api.search(
            query=params.query,
            memory_type=params.memory_type,
            scope=params.scope,
            limit=params.limit,
            search_mode=search_mode,
            embedding_provider=embedding_provider_obj,
        )
    else:
        memories = memory_api.query(
            memory_type=params.memory_type,
            scope=params.scope,
            state=params.state,
            limit=params.limit,
        )
    return {"memories": memories}


def handle_retrieve_memory(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle retrieve_memory RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    memory = memory_api.retrieve(
        namespace=params.namespace,
        path_key=params.path_key,
        path=params.path,
    )
    return {"memory": memory}


def handle_delete_memory(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle delete_memory RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    deleted = memory_api.delete(params.memory_id)
    return {"deleted": deleted}


def handle_approve_memory(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle approve_memory RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    approved = memory_api.approve(params.memory_id)
    return {"approved": approved}


def handle_deactivate_memory(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle deactivate_memory RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    deactivated = memory_api.deactivate(params.memory_id)
    return {"deactivated": deactivated}


def handle_approve_memory_batch(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle approve_memory_batch RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    result: dict[str, Any] = memory_api.approve_batch(params.memory_ids)
    return result


def handle_deactivate_memory_batch(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle deactivate_memory_batch RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    result: dict[str, Any] = memory_api.deactivate_batch(params.memory_ids)
    return result


def handle_delete_memory_batch(nexus_fs: NexusFS, params: Any, context: Any) -> dict[str, Any]:
    """Handle delete_memory_batch RPC method."""
    memory_api = _get_memory_api_with_context(nexus_fs, context)
    result: dict[str, Any] = memory_api.delete_batch(params.memory_ids)
    return result
