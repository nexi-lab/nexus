"""Driver-agnostic domain cache implementations built on CacheStoreABC.

These implement the domain protocols (PermissionCacheProtocol, TigerCacheProtocol)
using only CacheStoreABC primitives — no Redis/Dragonfly/PostgreSQL specifics.

Architecture:
    CacheStoreABC (low-level KV + PubSub)
        └── PermissionCache, TigerCache (domain logic ON TOP of primitives)

Any CacheStoreABC driver (Dragonfly, InMemory, Null) works transparently.
"""

from __future__ import annotations

import struct

from nexus.core.cache_store import CacheStoreABC


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
