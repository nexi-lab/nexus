"""Backend I/O service for connector content operations.

Extracted from CacheConnectorMixin (#1628). These methods are I/O
operations, not cache logic — they belong in a separate service.

Bug fixes applied:
    - parse_content(): Uses asyncio.run() instead of asyncio.new_event_loop()
      to avoid event loop leaks (1-5s overhead per 1000 files).

Part of: #1628 (Split CacheConnectorMixin into focused units)
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class BackendIOService:
    """Backend I/O operations for connector content.

    Encapsulates content parsing, embedding generation, and direct
    backend reads. Uses constructor injection (same pattern as
    SyncPipelineService).

    Args:
        connector: The connector instance providing backend access.
    """

    def __init__(self, connector: Any) -> None:
        self._connector = connector

    def parse_content(
        self,
        path: str,
        content: bytes,
    ) -> tuple[str | None, str | None, dict | None]:
        """Parse content using the parser registry.

        Args:
            path: File path (used to determine file type)
            content: Raw file content

        Returns:
            Tuple of (parsed_text, parsed_from, parse_metadata)
            Returns (None, None, None) if parsing fails or not supported
        """
        try:
            import importlib as _il

            MarkItDownParser = _il.import_module(
                "nexus.bricks.parsers.markitdown_parser"
            ).MarkItDownParser
        except ImportError:
            return None, None, None

        try:
            # Get file extension
            ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""

            # Check if parser supports this format
            parser = MarkItDownParser()
            if ext not in parser.supported_formats:
                return None, None, None

            # Parse content — use asyncio.run() (not new_event_loop)
            import asyncio

            result = asyncio.run(
                parser.parse(content, {"path": path, "filename": path.split("/")[-1]})
            )

            if result and result.text:
                return result.text, ext.lstrip("."), {"chunks": len(result.chunks)}

        except Exception as e:
            logger.debug("Content parsing failed for %s: %s", path, e)

        return None, None, None

    def generate_embeddings(self, path: str) -> None:
        """Generate embeddings for a file.

        Delegates to the connector's _generate_embeddings if available.
        Default implementation is a no-op.
        """
        if hasattr(self._connector, "_generate_embeddings"):
            self._connector._generate_embeddings(path)

    def batch_read_from_backend(
        self,
        paths: list[str],
        contexts: "dict[str, OperationContext] | None" = None,
    ) -> dict[str, bytes]:
        """Batch read content directly from backend (bypassing cache).

        Leverages _bulk_download_blobs() for efficient parallel downloads when
        available (PathBackend subclasses). Falls back to sequential
        reads for other connector types.

        Args:
            paths: List of backend-relative paths
            contexts: Optional dict mapping path -> OperationContext

        Returns:
            Dict mapping path -> content bytes (only successful reads)
        """
        connector = self._connector

        # Check if this connector has bulk download support
        if hasattr(connector, "_bulk_download_blobs") and hasattr(connector, "_get_blob_path"):
            logger.info(f"[BATCH-READ] Using bulk download for {len(paths)} paths")

            blob_paths = [connector._get_blob_path(path) for path in paths]

            version_ids: dict[str, str] = {}
            if contexts:
                for path in paths:
                    context = contexts.get(path)
                    if context and hasattr(context, "version_id") and context.version_id:
                        blob_path = connector._get_blob_path(path)
                        version_ids[blob_path] = context.version_id

            blob_results = connector._bulk_download_blobs(
                blob_paths,
                version_ids=version_ids if version_ids else None,
            )

            blob_to_backend = {connector._get_blob_path(p): p for p in paths}
            results: dict[str, bytes] = {}
            for blob_path, content in blob_results.items():
                backend_path = blob_to_backend.get(blob_path)
                if backend_path:
                    results[backend_path] = content

            logger.info(
                f"[BATCH-READ] Bulk download complete: {len(results)}/{len(paths)} successful"
            )
            return results

        # Check if this connector has custom bulk download support
        if hasattr(connector, "_bulk_download_contents"):
            logger.info(f"[BATCH-READ] Using connector bulk download for {len(paths)} paths")
            results = connector._bulk_download_contents(paths, contexts)
            logger.info(
                f"[BATCH-READ] Bulk download complete: {len(results)}/{len(paths)} successful"
            )
            return results

        # Fallback: sequential reads for non-blob connectors
        # Use connector's _read_content_from_backend (preserves MRO overrides)
        logger.info(f"[BATCH-READ] Falling back to sequential reads for {len(paths)} paths")
        results = {}
        for path in paths:
            context = contexts.get(path) if contexts else None
            content = connector._read_content_from_backend(path, context)
            if content is not None:
                results[path] = content
        return results

    def read_content_from_backend(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bytes | None:
        """Read content directly from backend (bypassing cache).

        Args:
            path: Backend-relative path
            context: Operation context with backend_path set
        """
        connector = self._connector

        # Try direct blob download first (bypasses cache in read_content)
        if hasattr(connector, "_download_blob") and hasattr(connector, "_get_blob_path"):
            try:
                blob_path = connector._get_blob_path(path)
                content: bytes = connector._download_blob(blob_path)
                return content
            except Exception as e:
                logger.debug("Direct blob download failed for %s: %s", path, e)

        # Fall back to read_content (may use cache)
        if hasattr(connector, "read_content"):
            try:
                return connector.read_content("", context)  # type: ignore
            except Exception as e:
                logger.debug("Fallback read_content failed for %s: %s", path, e)
                return None
        return None
