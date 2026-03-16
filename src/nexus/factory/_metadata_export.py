"""Metadata-export service factory — create_metadata_export_service for server-layer RPC."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def create_metadata_export_service(nx: Any) -> Any:
    """Create MetadataExportService for server-layer RPC dispatch (Issue #841).

    Args:
        nx: A NexusFS instance (used to read metadata store and context).

    Returns:
        MetadataExportService instance, or None if dependencies are unavailable.
    """
    try:
        from nexus.system_services.metadata_export import MetadataExportService

        metadata = getattr(nx, "metadata", None)
        if metadata is None:
            logger.debug("[FACTORY] MetadataExportService unavailable: no metadata store")
            return None

        # Build created_by string from default context
        created_by = None
        default_ctx = getattr(nx, "_default_context", None)
        if default_ctx is not None:
            parts = []
            user = getattr(default_ctx, "user_id", None)
            agent = getattr(default_ctx, "agent_id", None)
            if user:
                parts.append(f"user:{user}")
            if agent:
                parts.append(f"agent:{agent}")
            created_by = ",".join(parts) if parts else None

        svc = MetadataExportService(
            metadata=metadata,
            created_by=created_by,
        )
        logger.info("[FACTORY] MetadataExportService created for server-layer RPC")
        return svc
    except Exception as exc:
        logger.debug("[FACTORY] MetadataExportService unavailable: %s", exc)
        return None
