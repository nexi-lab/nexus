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


def _read_content(
    metadata: dict[str, Any],
    backend: Any,
    metastore: Any,
    path: str,
) -> bytes | None:
    """Read file content from inline storage or CAS backend."""
    from nexus.contracts.constants import INLINE_CONTENT_KEY, INLINE_PREFIX

    physical_path = metadata.get("physical_path", "")
    if physical_path.startswith(INLINE_PREFIX):
        # Inline: content stored base64-encoded in metastore.
        # NexusFS stores: set_file_metadata(path, key, b64encode(content).decode("ascii"))
        # NexusFS reads:  b64decode(get_file_metadata(path, key))
        import base64

        raw = metastore.get_file_metadata(path, INLINE_CONTENT_KEY)
        if raw is None:
            return None
        # NexusFS stores base64(content) in the metastore.  The Raft
        # metastore may add another base64 layer when storing string
        # values, producing base64(base64(content)).  Use the known
        # file size from metadata to detect whether a second decode
        # is needed: base64 always changes the length, so if the
        # first decode already matches the file size, it produced
        # the original content.  If not, decode again.
        expected_size = metadata.get("size", 0)
        content = base64.b64decode(raw)
        if expected_size and len(content) != expected_size:
            content = base64.b64decode(content)
        return content

    # CAS: content stored in backend by hash
    content_hash = metadata.get("etag")
    if not content_hash:
        return None
    result: bytes | None = backend.read_content(content_hash)
    return result


def make_extraction_hook(
    *,
    session_factory: Callable[..., Any],
    backend: Any,
    metastore: Any,
    max_extract_bytes: int = 100 * 1024 * 1024,
) -> Callable[[list[dict[str, Any]]], None]:
    """Create a post-flush hook for async-on-write extraction.

    Args:
        session_factory: RecordStore session factory for AspectService.
        backend: Storage backend with read_content(content_hash).
        metastore: Metastore for reading inline content.
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

                        # Read content: inline files are stored in the metastore,
                        # CAS files are stored in the backend.
                        content = _read_content(metadata, backend, metastore, path)
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
