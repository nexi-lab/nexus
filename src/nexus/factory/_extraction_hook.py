"""Async-on-write extraction hook — factory wiring for Issue #2978.

Creates a post-flush hook function that reads file content from the
backend and runs CatalogService.extract_auto() for supported formats.

The hook:
    1. Iterates flushed events, filtering for "write" ops
    2. Format-gates: checks extension against registered extractors
    3. Reads content from the backend via content hash (etag)
    4. Calls CatalogService.extract_auto() to store aspects
    5. Wraps all extractions in a single DB transaction with savepoints

Failures are logged and ignored — extraction is best-effort.
Recovery: ``nexus reindex --target semantic`` catches gaps.
"""

import logging
import mimetypes
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def make_extraction_hook(
    *,
    session_factory: Callable[..., Any],
    backend: Any,
    max_extract_bytes: int = 100 * 1024 * 1024,
) -> Callable[[list[dict[str, Any]]], None]:
    """Create a post-flush hook for async-on-write extraction.

    Args:
        session_factory: RecordStore session factory for AspectService.
        backend: Storage backend with read_content(content_hash).
        max_extract_bytes: Max file size for auto-extraction (100MB).

    Returns:
        Hook function: ``(events: list[dict]) -> None``
    """

    def extraction_hook(events: list[dict[str, Any]]) -> None:
        """Extract schemas/documents for written files (best-effort)."""
        # Filter to write events only
        write_events = [e for e in events if e.get("op") == "write"]
        if not write_events:
            return

        try:
            from nexus.bricks.catalog.protocol import CatalogService
            from nexus.contracts.urn import NexusURN
            from nexus.storage.aspect_service import AspectService

            with session_factory() as session:
                aspect_service = AspectService(session)
                catalog = CatalogService(aspect_service)

                extracted = 0
                for event in write_events:
                    try:
                        path = event.get("path", "")
                        zone_id = event.get("zone_id")
                        metadata = event.get("metadata", {})

                        # Derive filename for format detection
                        filename = path.rsplit("/", 1)[-1] if "/" in path else path
                        mime_type = metadata.get("mime_type") or None
                        if mime_type is None:
                            mime_type, _ = mimetypes.guess_type(filename)

                        # Format-gate: skip files with no registered extractor
                        if not catalog.has_extractor(mime_type=mime_type, filename=filename):
                            continue

                        # Size-gate: skip files too large for extraction
                        size = metadata.get("size", 0)
                        if size and size > max_extract_bytes:
                            continue

                        # Read content from backend via content hash.
                        # NOTE: This is a full-content read — the CAS backend
                        # returns bytes by hash, not a file path. Path-based
                        # O(1) extraction (Avro/Parquet header) is only possible
                        # in the reindex path where filesystem paths are available.
                        content_hash = metadata.get("etag")
                        if not content_hash:
                            continue

                        content = backend.read_content(content_hash)
                        if content is None:
                            continue

                        # Build URN and extract
                        urn = str(NexusURN.for_file(zone_id or "default", path))

                        # Savepoint isolation: one extraction failing
                        # doesn't roll back others (Issue #2978, Issue 16)
                        with session.begin_nested():
                            catalog.extract_auto(
                                entity_urn=urn,
                                content=content,
                                mime_type=mime_type,
                                filename=filename,
                                zone_id=zone_id,
                                created_by="auto-extract",
                            )
                            extracted += 1

                    except Exception:
                        logger.debug(
                            "Extraction failed for %s (non-critical)",
                            event.get("path"),
                            exc_info=True,
                        )

                if extracted > 0:
                    session.commit()
                    logger.debug(
                        "Post-flush extraction: %d/%d files extracted",
                        extracted,
                        len(write_events),
                    )

        except Exception:
            logger.debug(
                "Post-flush extraction batch failed (non-critical)",
                exc_info=True,
            )

    return extraction_hook
