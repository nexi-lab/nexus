"""Driver-agnostic domain cache implementations built on CacheStoreABC.

These implement the domain protocols (PermissionCacheProtocol, TigerCacheProtocol,
ResourceMapCacheProtocol, EmbeddingCacheProtocol) using only CacheStoreABC
primitives — no Redis/Dragonfly/PostgreSQL specifics.

Architecture:
    CacheStoreABC (low-level KV + PubSub)
        └── PermissionCache, TigerCache, ResourceMapCache, EmbeddingCache

Any CacheStoreABC driver (Dragonfly, InMemory, Null) works transparently.

NOTE: These are SERVICE-LEVEL domain caches, NOT kernel code.
    - Kernel only knows CacheStoreABC (the Pillar ABC) + NullCacheStore.
    - These classes live under core/cache/ as cache infrastructure, but the
      kernel (NexusFS) never imports them directly.
    - Only CacheFactory (systemd layer) and upper services (search, auth)
      consume these domain caches.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
from collections.abc import Awaitable, Callable

from nexus.core.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)


class PermissionCache:
    """Driver-agnostic permission cache using CacheStoreABC primitives.

    Implements PermissionCacheProtocol via structural subtyping.

    Key format:
        perm:{zone_id}:{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}

    Value:
        b"1" for grant, b"0" for denial
    """

    def __init__(
        self,
        store: CacheStoreABC,
        ttl: int = 300,
        denial_ttl: int = 60,
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._denial_ttl = denial_ttl

    def _make_key(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> str:
        return f"perm:{zone_id}:{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}"

    async def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool | None:
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)
        value = await self._store.get(key)
        if value is None:
            return None
        return value == b"1"

    async def set(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        zone_id: str,
    ) -> None:
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)
        ttl = self._ttl if result else self._denial_ttl
        await self._store.set(key, b"1" if result else b"0", ttl=ttl)

    async def invalidate_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> int:
        pattern = f"perm:{zone_id}:{subject_type}:{subject_id}:*"
        return await self._store.delete_by_pattern(pattern)

    async def invalidate_object(
        self,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        pattern = f"perm:{zone_id}:*:*:*:{object_type}:{object_id}"
        return await self._store.delete_by_pattern(pattern)

    async def invalidate_subject_object(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        pattern = f"perm:{zone_id}:{subject_type}:{subject_id}:*:{object_type}:{object_id}"
        return await self._store.delete_by_pattern(pattern)

    async def clear(self, zone_id: str | None = None) -> int:
        pattern = f"perm:{zone_id}:*" if zone_id else "perm:*"
        return await self._store.delete_by_pattern(pattern)

    async def health_check(self) -> bool:
        return await self._store.health_check()

    async def get_stats(self) -> dict:
        return {
            "backend": type(self._store).__name__,
            "ttl_grants": self._ttl,
            "ttl_denials": self._denial_ttl,
        }


class TigerCache:
    """Driver-agnostic Tiger cache using CacheStoreABC primitives.

    Implements TigerCacheProtocol via structural subtyping.

    Stores pre-materialized Roaring Bitmap data for O(1) list filtering.

    Key format:
        tiger:{zone_id}:{subject_type}:{subject_id}:{permission}:{resource_type}

    Value format (binary):
        [4 bytes: revision as big-endian uint32][remaining bytes: bitmap data]
    """

    def __init__(
        self,
        store: CacheStoreABC,
        ttl: int = 3600,
    ) -> None:
        self._store = store
        self._ttl = ttl

    def _make_key(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> str:
        return f"tiger:{zone_id}:{subject_type}:{subject_id}:{permission}:{resource_type}"

    async def get_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> tuple[bytes, int] | None:
        key = self._make_key(subject_type, subject_id, permission, resource_type, zone_id)
        value = await self._store.get(key)
        if value is None or len(value) < 4:
            return None
        revision = struct.unpack(">I", value[:4])[0]
        data = value[4:]
        return (data, revision)

    async def set_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        bitmap_data: bytes,
        revision: int,
    ) -> None:
        key = self._make_key(subject_type, subject_id, permission, resource_type, zone_id)
        value = struct.pack(">I", revision) + bitmap_data
        await self._store.set(key, value, ttl=self._ttl)

    async def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        parts = [
            zone_id or "*",
            subject_type or "*",
            subject_id or "*",
            permission or "*",
            resource_type or "*",
        ]
        pattern = f"tiger:{':'.join(parts)}"
        return await self._store.delete_by_pattern(pattern)

    async def health_check(self) -> bool:
        return await self._store.health_check()


class ResourceMapCache:
    """Driver-agnostic resource map cache using CacheStoreABC primitives.

    Implements ResourceMapCacheProtocol via structural subtyping.

    Maps resource UUIDs to integer IDs for Roaring Bitmap compatibility.

    Key format:
        resmap:{zone_id}:{resource_type}:{resource_id}

    Value:
        Integer ID encoded as ASCII bytes (e.g. b"42")
    """

    def __init__(self, store: CacheStoreABC) -> None:
        self._store = store

    def _make_key(
        self,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> str:
        return f"resmap:{zone_id}:{resource_type}:{resource_id}"

    async def get_int_id(
        self,
        resource_type: str,
        resource_id: str,
        zone_id: str,
    ) -> int | None:
        key = self._make_key(resource_type, resource_id, zone_id)
        value = await self._store.get(key)
        if value is None:
            return None
        return int(value)

    async def get_int_ids_bulk(
        self,
        resources: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], int | None]:
        if not resources:
            return {}
        keys = [self._make_key(rt, rid, zid) for rt, rid, zid in resources]
        raw = await self._store.get_many(keys)
        results: dict[tuple[str, str, str], int | None] = {}
        for resource, key in zip(resources, keys, strict=True):
            value = raw[key]
            results[resource] = int(value) if value is not None else None
        return results

    async def set_int_id(
        self,
        resource_type: str,
        resource_id: str,
        zone_id: str,
        int_id: int,
    ) -> None:
        key = self._make_key(resource_type, resource_id, zone_id)
        await self._store.set(key, str(int_id).encode())

    async def set_int_ids_bulk(
        self,
        mappings: dict[tuple[str, str, str], int],
    ) -> None:
        if not mappings:
            return
        kv = {
            self._make_key(rt, rid, zid): str(int_id).encode()
            for (rt, rid, zid), int_id in mappings.items()
        }
        await self._store.set_many(kv)


class EmbeddingCache:
    """Driver-agnostic embedding cache using CacheStoreABC primitives.

    Implements EmbeddingCacheProtocol via structural subtyping.

    Caches embedding vectors by content hash to avoid redundant API calls.

    Key format:
        emb:v1:{sha256(model:text)[:32]}

    Value:
        JSON-serialized embedding vector as bytes
    """

    CACHE_VERSION = "v1"

    def __init__(
        self,
        store: CacheStoreABC,
        ttl: int = 86400,
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._hits = 0
        self._misses = 0
        self._errors = 0

    def _content_hash(self, text: str, model: str) -> str:
        content = f"{model}:{text}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def _make_key(self, text: str, model: str) -> str:
        return f"emb:{self.CACHE_VERSION}:{model}:{self._content_hash(text, model)}"

    async def get(self, text: str, model: str) -> list[float] | None:
        key = self._make_key(text, model)
        try:
            cached = await self._store.get(key)
            if cached is not None:
                self._hits += 1
                return json.loads(cached)
            self._misses += 1
            return None
        except Exception as e:
            logger.warning(f"Embedding cache get failed: {e}")
            self._errors += 1
            return None

    async def set(self, text: str, model: str, embedding: list[float]) -> None:
        key = self._make_key(text, model)
        try:
            await self._store.set(key, json.dumps(embedding).encode(), ttl=self._ttl)
        except Exception as e:
            logger.warning(f"Embedding cache set failed: {e}")
            self._errors += 1

    async def get_batch(
        self,
        texts: list[str],
        model: str,
    ) -> dict[str, list[float] | None]:
        if not texts:
            return {}
        keys = [self._make_key(text, model) for text in texts]
        try:
            raw = await self._store.get_many(keys)
            results: dict[str, list[float] | None] = {}
            for text, key in zip(texts, keys, strict=True):
                value = raw[key]
                if value is not None:
                    self._hits += 1
                    results[text] = json.loads(value)
                else:
                    self._misses += 1
                    results[text] = None
            return results
        except Exception as e:
            logger.warning(f"Embedding cache batch get failed: {e}")
            self._errors += 1
            return dict.fromkeys(texts, None)

    async def set_batch(
        self,
        embeddings: dict[str, list[float]],
        model: str,
    ) -> None:
        if not embeddings:
            return
        try:
            kv = {
                self._make_key(text, model): json.dumps(emb).encode()
                for text, emb in embeddings.items()
            }
            await self._store.set_many(kv, ttl=self._ttl)
        except Exception as e:
            logger.warning(f"Embedding cache batch set failed: {e}")
            self._errors += 1

    async def get_or_embed_batch(
        self,
        texts: list[str],
        model: str,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]],
    ) -> list[list[float]]:
        if not texts:
            return []

        # Deduplicate while preserving order
        unique_texts = list(dict.fromkeys(texts))

        # Check cache for all unique texts
        cached = await self.get_batch(unique_texts, model)

        # Find uncached texts
        uncached_texts = [t for t in unique_texts if cached[t] is None]

        # Generate embeddings for uncached texts only
        if uncached_texts:
            logger.info(
                f"Embedding cache: {len(unique_texts) - len(uncached_texts)}/{len(unique_texts)} "
                f"hits, generating {len(uncached_texts)} new embeddings"
            )
            new_embeddings = await embed_fn(uncached_texts)
            new_entries = dict(zip(uncached_texts, new_embeddings, strict=True))
            await self.set_batch(new_entries, model)
            for text, embedding in new_entries.items():
                cached[text] = embedding
        else:
            logger.info(f"Embedding cache: 100% hit rate ({len(unique_texts)} texts)")

        # Build result in original order (handling duplicates)
        return [cached[text] for text in texts]  # type: ignore[misc]

    async def invalidate(self, text: str, model: str) -> bool:
        key = self._make_key(text, model)
        try:
            return await self._store.delete(key)
        except Exception as e:
            logger.warning(f"Embedding cache invalidate failed: {e}")
            self._errors += 1
            return False

    async def clear(self, model: str | None = None) -> int:
        pattern = f"emb:{self.CACHE_VERSION}:{model}:*" if model else f"emb:{self.CACHE_VERSION}:*"
        return await self._store.delete_by_pattern(pattern)

    async def health_check(self) -> bool:
        return await self._store.health_check()

    def get_metrics(self) -> dict:
        total = self._hits + self._misses
        hit_rate = self._hits / total if total > 0 else 0.0
        tokens_saved = self._hits * 500
        cost_saved = (tokens_saved / 1_000_000) * 0.13
        return {
            "hits": self._hits,
            "misses": self._misses,
            "errors": self._errors,
            "hit_rate": round(hit_rate, 4),
            "estimated_tokens_saved": tokens_saved,
            "estimated_cost_saved_usd": round(cost_saved, 4),
            "ttl_seconds": self._ttl,
        }
