"""CAS Garbage Collector — reachability-based background service.

Two-phase GC:
  Phase 1 (collect): Scan the namespace via sys_readdir → build set of all
          referenced content_ids. For CDC manifests, parse the manifest →
          add chunk hashes to the referenced set.
  Phase 2 (sweep):   Enumerate CAS blobs via transport.list_content_hashes(),
          delete unreferenced blobs older than the grace period.

The collector is a service (BackgroundService), not a driver-owned helper.
The factory enlists one instance when the root backend is CAS; nexus-cluster
(Rust binary, no Python factory) is naturally unaffected.

Design:
    - Grace period: uses write timestamp from transport (volume index or file mtime)
    - Scan interval is configurable (default 60s)
    - Implements BackgroundService — async start/stop, idempotent
    - Thread-safe: blob deletion is idempotent (already-deleted = no-op)
    - Transport-agnostic: works with both file-per-blob and volume-packed storage

Issue #1320: CAS async GC.
Issue #1772: Reachability-based GC replacing ref_count.
Issue #3403: Transport-agnostic GC for volume packing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.base.cas_addressing_engine import CASAddressingEngine

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_GRACE_PERIOD_S = 300.0  # 5 minutes
DEFAULT_SCAN_INTERVAL_S = 60.0  # 1 minute


class CasGcService:
    """Reachability-based GC for CAS blobs (BackgroundService).

    Usage::

        gc = CasGcService(engine, nexus_fs)
        await gc.start()   # spawns asyncio.Task
        ...
        await gc.stop()    # cancels task, waits for clean exit
    """

    def __init__(
        self,
        engine: CASAddressingEngine,
        nexus_fs: Any,
        *,
        grace_period: float = DEFAULT_GRACE_PERIOD_S,
        scan_interval: float = DEFAULT_SCAN_INTERVAL_S,
    ) -> None:
        self._engine = engine
        # NexusFS handle — the reachability scan walks the global namespace
        # through the Tier 1 sys_readdir syscall, never the
        # metastore_list_paginated kernel primitive.
        self._nexus_fs = nexus_fs
        self._grace_period = grace_period
        self._scan_interval = scan_interval
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        """Start the GC background task (BackgroundService entry point)."""
        if self._task is not None:
            return
        self._stopped = False
        self._task = asyncio.ensure_future(self._run())
        logger.info(
            "CAS GC started for %s (grace=%ds, interval=%ds)",
            self._engine.name,
            self._grace_period,
            self._scan_interval,
        )

    async def stop(self) -> None:
        """Stop the GC background task."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("CAS GC stopped for %s", self._engine.name)

    async def _run(self) -> None:
        """Main GC loop — scan + collect on interval."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._scan_interval)
                if self._stopped:
                    break
                self._collect()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("CAS GC scan error for %s", self._engine.name, exc_info=True)

    def _collect(self) -> None:
        """Single GC pass — two-phase reachability scan.

        Phase 1: Walk the namespace via sys_readdir(details=True) to collect
                 all referenced content_ids. For CDC manifests, expand to
                 include chunk hashes.
        Phase 2: Enumerate all CAS blobs via transport.list_content_hashes(),
                 delete unreferenced blobs older than the grace period.
                 Transport-agnostic — works with both file-per-blob and
                 volume-packed storage (Issue #3403).
        """
        if self._nexus_fs is None:
            logger.debug("CAS GC: nexus_fs not set, skipping collection")
            return

        engine = self._engine
        transport = engine._transport
        now = time.time()

        # Phase 1: Collect referenced content_ids from the namespace.
        referenced: set[str] = set()
        try:
            self._scan_namespace(referenced)
        except Exception:
            logger.warning("CAS GC: namespace scan failed for %s", engine.name, exc_info=True)
            return

        # Phase 2: Sweep CAS blobs — transport-agnostic enumeration.
        try:
            if hasattr(transport, "list_content_hashes"):
                # Preferred: transport provides (hash, timestamp) pairs directly
                content_entries = transport.list_content_hashes()
            else:
                # Legacy fallback: walk filesystem via list_keys
                content_entries = self._list_keys_fallback(transport)
        except Exception:
            logger.debug("CAS GC: enumeration failed for %s", engine.name, exc_info=True)
            return

        # Issue #3406: resolve tiering manifest for skip check.
        # GC must not delete blobs in TIERING or TIERED volumes.
        tiering_manifest = None
        if hasattr(transport, "tiering") and transport.tiering is not None:
            tiering_manifest = transport.tiering.manifest

        collected = 0
        for content_hash, write_time in content_entries:
            if content_hash in referenced:
                continue

            # Unreferenced — check grace period
            if write_time > 0 and (now - write_time) < self._grace_period:
                continue  # Too fresh — within grace period

            # Issue #3406: O(1) skip for blobs in tiered/tiering volumes.
            # Uses the manifest's reverse hash set (built from per-volume
            # .idx files at load time).
            if tiering_manifest is not None and tiering_manifest.is_hash_tiered(content_hash):
                continue

            # Delete blob + meta sidecar
            blob_key = engine._blob_key(content_hash)
            with contextlib.suppress(Exception):
                transport.remove(blob_key)
            meta_key = engine._meta_key(content_hash)
            with contextlib.suppress(Exception):
                transport.remove(meta_key)

            # Evict from meta cache
            if engine._meta_cache is not None:
                engine._meta_cache.pop(content_hash, None)

            collected += 1

        if collected > 0:
            logger.info("CAS GC: collected %d unreferenced blobs for %s", collected, engine.name)

    @staticmethod
    def _list_keys_fallback(transport: Any) -> list[tuple[str, float]]:
        """Legacy fallback: enumerate blobs via list_keys + mtime.

        Used when transport doesn't support list_content_hashes().
        """
        blob_keys, _ = transport.list_keys(prefix="cas/", delimiter="")
        entries: list[tuple[str, float]] = []
        for blob_key in blob_keys:
            if blob_key.endswith(".meta"):
                continue
            content_hash = blob_key.split("/")[-1]
            try:
                mtime = transport.get_mtime(blob_key) if hasattr(transport, "get_mtime") else 0.0
            except Exception:
                mtime = 0.0
            entries.append((content_hash, mtime))
        return entries

    def _scan_namespace(self, referenced: set[str]) -> None:
        """Scan the namespace to collect all referenced content_ids.

        For CDC manifests (is_chunked_manifest in .meta), parse the manifest
        blob to add individual chunk hashes to the referenced set.
        """
        engine = self._engine

        # Tier 1 syscall — sys_readdir(details=True) yields full FileMetadata
        # projection as JSON-safe dicts, including each entry's content_id.
        try:
            all_entries = self._nexus_fs.sys_readdir("/", recursive=True, details=True)
        except Exception:
            logger.warning("CAS GC: nexus_fs.sys_readdir() failed", exc_info=True)
            return

        for entry in all_entries:
            content_id = entry.get("content_id")
            if not content_id:
                continue
            referenced.add(content_id)

            # Expand CDC manifests → add chunk hashes
            try:
                meta = engine._read_meta(content_id)
                if meta.get("is_chunked_manifest"):
                    self._expand_manifest(content_id, referenced)
            except Exception:
                pass  # Skip broken entries

    def _expand_manifest(self, manifest_hash: str, referenced: set[str]) -> None:
        """Parse a CDC manifest and add all chunk hashes to referenced set."""
        engine = self._engine
        key = engine._blob_key(manifest_hash)
        try:
            manifest_data, _ = engine._transport.fetch(key)
            manifest: dict[str, Any] = json.loads(manifest_data)
            for chunk in manifest.get("chunks", []):
                chunk_hash = chunk.get("chunk_hash")
                if chunk_hash:
                    referenced.add(chunk_hash)
        except Exception:
            logger.debug("CAS GC: failed to expand manifest %s", manifest_hash[:16])
