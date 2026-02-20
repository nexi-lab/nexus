"""Artifact content extraction (Issue #1861).

Extracts text from A2A Artifact parts using duck-typed ``part.type``
discriminator (avoids cross-brick imports per LEGO architecture).
Truncates at ``max_content_bytes`` with a logged warning.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from nexus.bricks.artifact_index.protocol import ArtifactContent

logger = logging.getLogger(__name__)


def extract_content(
    artifact: Any,
    task_id: str,
    zone_id: str,
    max_bytes: int = 100_000,
) -> ArtifactContent:
    """Extract indexable text from an artifact's parts.

    Dispatch rules (duck-typed ``part.type`` discriminator):
    - ``type="text"`` → ``part.text``
    - ``type="file"`` → ``"file://<url>"`` or ``"file:<name>"``
    - ``type="data"`` → ``json.dumps(part.data)``

    Parts are joined with newlines.  The result is truncated at
    *max_bytes* (UTF-8 encoded length) with a warning.

    Args:
        artifact: An A2A Artifact (duck-typed, has artifactId, parts, etc.).
        task_id: Owning task ID (included in returned content).
        zone_id: Zone scope (included in returned content).
        max_bytes: Maximum UTF-8 byte length before truncation.

    Returns:
        An ``ArtifactContent`` with extracted text and merged metadata.
    """
    segments: list[str] = []
    merged_metadata: dict[str, Any] = {}

    artifact_metadata = getattr(artifact, "metadata", None)
    if artifact_metadata:
        merged_metadata.update(artifact_metadata)
    artifact_name = getattr(artifact, "name", None)
    if artifact_name:
        merged_metadata["artifact_name"] = artifact_name
    artifact_desc = getattr(artifact, "description", None)
    if artifact_desc:
        merged_metadata["artifact_description"] = artifact_desc

    for part in artifact.parts:
        part_type = getattr(part, "type", None)

        if part_type == "text":
            segments.append(part.text)
        elif part_type == "file":
            file_obj = part.file
            uri = (
                getattr(file_obj, "url", None)
                or f"file:{getattr(file_obj, 'name', None) or 'unknown'}"
            )
            segments.append(uri)
        elif part_type == "data":
            try:
                segments.append(json.dumps(part.data, ensure_ascii=False))
            except (TypeError, ValueError):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "[ARTIFACT-INDEX] Failed to serialize DataPart for artifact %s",
                        artifact.artifactId,
                    )

        part_metadata = getattr(part, "metadata", None)
        if part_metadata:
            merged_metadata.update(part_metadata)

    text = "\n".join(segments)

    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        logger.warning(
            "[ARTIFACT-INDEX] Truncating artifact %s content from %d to %d bytes",
            artifact.artifactId,
            len(encoded),
            max_bytes,
        )
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")

    return ArtifactContent(
        text=text,
        metadata=merged_metadata,
        artifact_id=artifact.artifactId,
        task_id=task_id,
        zone_id=zone_id,
    )
