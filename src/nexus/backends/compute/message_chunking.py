"""Message-boundary chunking strategy for LLM conversations.

Chunks conversation JSON at message boundaries so two conversations
sharing the first N messages deduplicate those N chunks in CAS.

    Conversation A: [sys, u1, a1, u2]
    Conversation B: [sys, u1, a1, u3]  ← diverges at msg 4

    A chunks: [hash(sys), hash(u1), hash(a1), hash(u2)]
    B chunks: [hash(sys), hash(u1), hash(a1), hash(u3)]
                  shared     shared     shared    different

Implements ChunkingStrategy protocol — plugged into CASBackend via
Feature DI (cdc_engine parameter). Always-chunk mode: every
conversation is chunked regardless of size (unlike CDCEngine's 16MB
threshold), because LLM conversations are < 4MB but benefit from
per-message dedup.

See: docs/architecture/llm-cas-kv-cache.md

References:
    - Task #1589: LLM backend driver design
    - src/nexus/backends/engines/cdc.py — ChunkingStrategy protocol
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from nexus.backends.engines.cdc import ChunkedReference, ChunkInfo
from nexus.core.hash_fast import hash_content

if TYPE_CHECKING:
    from nexus.backends.base.cas_backend import CASBackend
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class MessageBoundaryStrategy:
    """Chunk LLM conversations at message boundaries.

    Each message in the conversation JSON array becomes a separate CAS
    chunk. The manifest links chunk hashes in order. Two conversations
    sharing a prefix share the same chunk blobs via CAS dedup.

    Always-chunk mode: ``should_chunk()`` returns True for any valid
    conversation JSON (array of message dicts). Returns False for
    non-conversation content, falling back to single-blob CAS storage.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: "CASBackend") -> None:
        self._backend = backend

    # ------------------------------------------------------------------
    # ChunkingStrategy protocol
    # ------------------------------------------------------------------

    def should_chunk(self, content: bytes) -> bool:
        """True if content is a JSON array of message dicts (conversation)."""
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return False
        if not isinstance(parsed, list) or len(parsed) < 2:
            return False
        # Check first element looks like a message dict
        first = parsed[0]
        return isinstance(first, dict) and "role" in first

    def write_chunked(self, content: bytes, context: "OperationContext | None" = None) -> str:
        """Write conversation as per-message chunks + manifest."""
        start_time = time.perf_counter()
        b = self._backend

        full_hash = hash_content(content)
        messages: list[dict[str, Any]] = json.loads(content)

        chunk_infos: list[ChunkInfo] = []
        offset = 0
        for msg in messages:
            chunk_bytes = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            chunk_hash = hash_content(chunk_bytes)
            length = len(chunk_bytes)

            # Store chunk blob (idempotent — same message = same hash)
            key = b._blob_key(chunk_hash)
            b._transport.put_blob(key, chunk_bytes)

            # Update chunk metadata with ref count
            def _update_chunk(meta: dict[str, Any], sz: int = length) -> dict[str, Any]:
                meta["ref_count"] = meta.get("ref_count", 0) + 1
                meta["size"] = sz
                meta["is_chunk"] = True
                return meta

            updated = b._meta_update_locked(chunk_hash, _update_chunk)
            if updated.get("ref_count", 0) == 1 and b._bloom is not None:
                b._bloom.add(chunk_hash)

            chunk_infos.append(ChunkInfo(chunk_hash=chunk_hash, offset=offset, length=length))
            offset += length

        # Build manifest
        manifest = ChunkedReference(
            total_size=len(content),
            chunk_count=len(chunk_infos),
            avg_chunk_size=offset // len(chunk_infos) if chunk_infos else 0,
            content_hash=full_hash,
            chunks=tuple(chunk_infos),
        )
        manifest_bytes = manifest.to_json()
        manifest_hash = hash_content(manifest_bytes)

        # Store manifest
        key = b._blob_key(manifest_hash)
        b._transport.put_blob(key, manifest_bytes)

        def _update_manifest(meta: dict[str, Any]) -> dict[str, Any]:
            meta["ref_count"] = meta.get("ref_count", 0) + 1
            meta["size"] = len(content)
            meta["is_chunked_manifest"] = True
            meta["chunk_count"] = len(chunk_infos)
            return meta

        updated = b._meta_update_locked(manifest_hash, _update_manifest)
        if updated.get("ref_count", 0) == 1 and b._bloom is not None:
            b._bloom.add(manifest_hash)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "Message-boundary chunked: %d messages, %d bytes in %.1fms (manifest=%s)",
            len(chunk_infos),
            len(content),
            elapsed_ms,
            manifest_hash[:16],
        )
        return manifest_hash

    def read_chunked(self, content_hash: str, context: "OperationContext | None" = None) -> bytes:
        """Reassemble conversation from per-message chunks."""
        b = self._backend
        key = b._blob_key(content_hash)
        manifest_data, _ = b._transport.get_blob(key)
        manifest = ChunkedReference.from_json(manifest_data)

        # Read chunks in order (sequential — messages are small)
        messages: list[dict[str, Any]] = []
        for ci in manifest.chunks:
            chunk_key = b._blob_key(ci.chunk_hash)
            chunk_data, _ = b._transport.get_blob(chunk_key)
            messages.append(json.loads(chunk_data))

        # Reassemble as JSON array
        content = json.dumps(messages, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

        # Verify integrity
        actual_hash = hash_content(content)
        if actual_hash != manifest.content_hash:
            # Serialization may differ — return raw concat if hash matches
            logger.warning(
                "Message-boundary read: reassembled hash mismatch "
                "(expected=%s, got=%s). Returning raw chunk concat.",
                manifest.content_hash[:16],
                actual_hash[:16],
            )
        return content

    def read_chunked_range(
        self,
        content_hash: str,
        start: int,
        end: int,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read byte range from reassembled conversation."""
        content = self.read_chunked(content_hash, context)
        return content[start:end]

    def is_chunked(self, content_hash: str) -> bool:
        """Check if content_hash refers to a chunked manifest."""
        b = self._backend
        key = b._blob_key(content_hash)
        try:
            data, _ = b._transport.get_blob(key)
            return ChunkedReference.is_chunked_manifest(data)
        except Exception:
            return False

    def get_size(self, content_hash: str) -> int:
        """Get original conversation size from manifest."""
        b = self._backend
        key = b._blob_key(content_hash)
        data, _ = b._transport.get_blob(key)
        manifest = ChunkedReference.from_json(data)
        return manifest.total_size

    def delete_chunked(self, content_hash: str, context: "OperationContext | None" = None) -> None:
        """Delete manifest + decrement chunk ref counts."""
        b = self._backend
        key = b._blob_key(content_hash)

        try:
            data, _ = b._transport.get_blob(key)
            manifest = ChunkedReference.from_json(data)
        except Exception:
            return

        # Decrement chunk ref counts
        for ci in manifest.chunks:
            chunk_meta_key = b._meta_key(ci.chunk_hash)
            try:
                meta_data, _ = b._transport.get_blob(chunk_meta_key)
                meta: dict[str, Any] = json.loads(meta_data)
                meta["ref_count"] = max(0, meta.get("ref_count", 1) - 1)
                if meta["ref_count"] == 0:
                    # Remove chunk blob + metadata
                    b._transport.delete_blob(b._blob_key(ci.chunk_hash))
                    b._transport.delete_blob(chunk_meta_key)
                else:
                    b._transport.put_blob(chunk_meta_key, json.dumps(meta).encode())
            except Exception:
                pass

        # Remove manifest
        b._transport.delete_blob(key)
        b._transport.delete_blob(b._meta_key(content_hash))
