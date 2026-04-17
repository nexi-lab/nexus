"""Distributed lease manager — Dragonfly-backed LeaseManagerProtocol.

Implements ``LeaseManagerProtocol`` with Dragonfly/Redis as the shared
state store, enabling cross-zone lease coordination.

Lease state is stored as Redis hashes with TTL:
    Key: ``lease:{zone_id}:{resource_id}:{holder_id}``
    Fields: state, generation, granted_at, expires_at

Atomic acquire uses a Lua script that checks compatibility, revokes
conflicts, and grants in a single round-trip.

References:
    - DFUSE paper: https://arxiv.org/abs/2503.18191
    - Issue #3396: Cross-zone lease-based cache invalidation
    - Issue #3407: Common LeaseManager utility
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.lease import (
    Lease,
    LeaseState,
    RevocationCallback,
)

logger = logging.getLogger(__name__)

_CALLBACK_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# Lua scripts for atomic operations
# ---------------------------------------------------------------------------

# Atomic acquire: check compatibility, revoke conflicts, grant.
# KEYS[1] = resource index key (hash: holder_id -> lease JSON)
# ARGV[1] = holder_id
# ARGV[2] = requested state ("shared" or "exclusive")
# ARGV[3] = ttl (seconds, float)
# ARGV[4] = now (monotonic, float — but stored as wall clock for TTL)
# ARGV[5] = generation (pre-computed by caller)
# ARGV[6] = resource_id
#
# Returns: JSON array of [granted_lease_json, [revoked_lease_json, ...]]
#          or nil if conflict with timeout=0 semantics (caller handles)
_LUA_ACQUIRE = """
local rkey = KEYS[1]
local holder_id = ARGV[1]
local req_state = ARGV[2]
local ttl = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local generation = tonumber(ARGV[5])
local resource_id = ARGV[6]

-- Get all current holders
local holders = redis.call('HGETALL', rkey)
local conflicts = {}
local existing_same = nil

for i = 1, #holders, 2 do
    local hid = holders[i]
    local lease_json = holders[i + 1]
    local lease = cjson.decode(lease_json)

    -- Skip expired leases (clean up lazily)
    if tonumber(lease.expires_at) <= now then
        redis.call('HDEL', rkey, hid)
    elseif hid == holder_id then
        existing_same = lease
    else
        -- Check compatibility
        local compat = (lease.state == 'shared' and req_state == 'shared')
        if not compat then
            table.insert(conflicts, {hid, lease_json})
        end
    end
end

-- Same holder, same state: extend TTL (idempotent)
if existing_same and existing_same.state == req_state then
    existing_same.expires_at = now + ttl
    local updated_json = cjson.encode(existing_same)
    redis.call('HSET', rkey, holder_id, updated_json)
    redis.call('EXPIRE', rkey, math.ceil(ttl) + 5)
    return cjson.encode({updated_json, {}})
end

-- Same holder, different state: remove old (upgrade/downgrade)
if existing_same then
    redis.call('HDEL', rkey, holder_id)
end

-- Revoke conflicts
local revoked = {}
for _, conflict in ipairs(conflicts) do
    local hid = conflict[1]
    local lease_json = conflict[2]
    redis.call('HDEL', rkey, hid)
    table.insert(revoked, lease_json)
end

-- Grant new lease
local new_lease = {
    resource_id = resource_id,
    holder_id = holder_id,
    state = req_state,
    generation = generation,
    granted_at = now,
    expires_at = now + ttl
}
local new_json = cjson.encode(new_lease)
redis.call('HSET', rkey, holder_id, new_json)
redis.call('EXPIRE', rkey, math.ceil(ttl) + 5)

return cjson.encode({new_json, revoked})
"""


class DistributedLeaseManager:
    """Dragonfly-backed distributed lease manager.

    Provides cross-zone lease coordination via shared Redis/Dragonfly state.
    Implements ``LeaseManagerProtocol``.

    zone_id is bound at construction — callers never pass it per-method.

    Example::

        mgr = DistributedLeaseManager(
            redis_client=async_redis,
            zone_id="us-east-1",
        )
        lease = await mgr.acquire("file:123", "agent-A", LeaseState.SHARED_READ)
    """

    DEFAULT_TTL = 30.0
    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        redis_client: Any,
        *,
        zone_id: str = ROOT_ZONE_ID,
        key_prefix: str = "lease",
        callback_timeout: float = _CALLBACK_TIMEOUT_S,
    ) -> None:
        self._client = redis_client
        self._zone_id = zone_id
        self._key_prefix = key_prefix
        self._callback_timeout = callback_timeout

        # Revocation callbacks
        self._callbacks: list[tuple[str, RevocationCallback]] = []

        # Per-resource generation counter (cached locally, authoritative in Redis)
        self._generation_cache: dict[str, int] = {}

        # Stats
        self._acquire_count = 0
        self._revoke_count = 0
        self._extend_count = 0
        self._timeout_count = 0
        self._callback_error_count = 0

        # Lua script SHA (cached after first SCRIPT LOAD)
        self._acquire_sha: str | None = None

    # -- key helpers ----------------------------------------------------------

    def _resource_key(self, resource_id: str) -> str:
        """Redis key for a resource's lease holders."""
        return f"{self._key_prefix}:{self._zone_id}:{resource_id}"

    def _generation_key(self, resource_id: str) -> str:
        """Redis key for a resource's generation counter."""
        return f"{self._key_prefix}:gen:{self._zone_id}:{resource_id}"

    async def _next_generation(self, resource_id: str) -> int:
        """Atomically increment and return the generation for a resource."""
        gen_key = self._generation_key(resource_id)
        gen = await self._client.incr(gen_key)
        return int(gen)

    # -- Lua script management ------------------------------------------------

    async def _load_scripts(self) -> None:
        """Load Lua scripts into Redis (cached by SHA)."""
        if self._acquire_sha is None:
            self._acquire_sha = await self._client.script_load(_LUA_ACQUIRE)

    # -- public API -----------------------------------------------------------

    async def acquire(
        self,
        resource_id: str,
        holder_id: str,
        state: LeaseState,
        *,
        ttl: float = DEFAULT_TTL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Lease | None:
        """Acquire a lease via atomic Lua script."""
        await self._load_scripts()

        rkey = self._resource_key(resource_id)
        deadline = time.monotonic() + timeout
        retry_interval = 0.05

        while True:
            now = time.monotonic()
            if timeout > 0 and now >= deadline:
                self._timeout_count += 1
                return None

            generation = await self._next_generation(resource_id)

            try:
                result = await self._client.evalsha(
                    self._acquire_sha,
                    1,
                    rkey,
                    holder_id,
                    state.value,
                    str(ttl),
                    str(now),
                    str(generation),
                    resource_id,
                )
            except Exception:
                logger.warning(
                    "[DistributedLease] Acquire script failed for %s",
                    resource_id,
                    exc_info=True,
                )
                if timeout <= 0:
                    return None
                await asyncio.sleep(retry_interval)
                retry_interval = min(retry_interval * 2, 1.0)
                continue

            if result is None:
                if timeout <= 0:
                    self._timeout_count += 1
                    return None
                await asyncio.sleep(retry_interval)
                retry_interval = min(retry_interval * 2, 1.0)
                continue

            parsed = json.loads(result if isinstance(result, str) else result.decode())
            granted_json, revoked_jsons = parsed

            # Parse granted lease
            granted_data = (
                json.loads(granted_json) if isinstance(granted_json, str) else granted_json
            )
            lease = Lease(
                resource_id=granted_data["resource_id"],
                holder_id=granted_data["holder_id"],
                state=LeaseState(granted_data["state"]),
                generation=int(granted_data["generation"]),
                granted_at=float(granted_data["granted_at"]),
                expires_at=float(granted_data["expires_at"]),
            )

            # Invoke revocation callbacks for conflicts
            revoked_leases = []
            for rj in revoked_jsons:
                rd = json.loads(rj) if isinstance(rj, str) else rj
                revoked_leases.append(
                    Lease(
                        resource_id=rd.get("resource_id", resource_id),
                        holder_id=rd["holder_id"],
                        state=LeaseState(rd["state"]),
                        generation=int(rd["generation"]),
                        granted_at=float(rd["granted_at"]),
                        expires_at=float(rd["expires_at"]),
                    )
                )

            if revoked_leases:
                await self._invoke_callbacks(revoked_leases, "conflict")
                self._revoke_count += len(revoked_leases)

            self._acquire_count += 1
            return lease

    async def validate(
        self,
        resource_id: str,
        holder_id: str,
    ) -> Lease | None:
        """Check if a lease is still valid."""
        rkey = self._resource_key(resource_id)
        raw = await self._client.hget(rkey, holder_id)
        if raw is None:
            return None

        data = json.loads(raw if isinstance(raw, str) else raw.decode())
        now = time.monotonic()
        if float(data["expires_at"]) <= now:
            # Expired — clean up lazily
            await self._client.hdel(rkey, holder_id)
            return None

        return Lease(
            resource_id=data["resource_id"],
            holder_id=data["holder_id"],
            state=LeaseState(data["state"]),
            generation=int(data["generation"]),
            granted_at=float(data["granted_at"]),
            expires_at=float(data["expires_at"]),
        )

    async def revoke(
        self,
        resource_id: str,
        *,
        holder_id: str | None = None,
    ) -> list[Lease]:
        """Revoke leases on a resource."""
        rkey = self._resource_key(resource_id)
        revoked: list[Lease] = []

        if holder_id is not None:
            raw = await self._client.hget(rkey, holder_id)
            if raw:
                data = json.loads(raw if isinstance(raw, str) else raw.decode())
                revoked.append(self._parse_lease(data, resource_id))
                await self._client.hdel(rkey, holder_id)
        else:
            all_holders = await self._client.hgetall(rkey)
            for _hid, raw in all_holders.items():
                data = json.loads(raw if isinstance(raw, str) else raw.decode())
                revoked.append(self._parse_lease(data, resource_id))
            if revoked:
                await self._client.delete(rkey)

        if revoked:
            self._revoke_count += len(revoked)
            await self._invoke_callbacks(revoked, "explicit")

        return revoked

    async def revoke_holder(self, holder_id: str) -> list[Lease]:
        """Revoke all leases owned by a holder.

        Note: This requires scanning — use sparingly (e.g. on disconnect).
        """
        pattern = f"{self._key_prefix}:{self._zone_id}:*"
        revoked: list[Lease] = []

        async for key in self._client.scan_iter(match=pattern, count=100):
            key_str = key.decode() if isinstance(key, bytes) else key
            raw = await self._client.hget(key_str, holder_id)
            if raw:
                data = json.loads(raw if isinstance(raw, str) else raw.decode())
                resource_id = data.get("resource_id", key_str.split(":", 2)[-1])
                revoked.append(self._parse_lease(data, resource_id))
                await self._client.hdel(key_str, holder_id)

        if revoked:
            self._revoke_count += len(revoked)
            await self._invoke_callbacks(revoked, "explicit")

        return revoked

    async def extend(
        self,
        resource_id: str,
        holder_id: str,
        *,
        ttl: float = DEFAULT_TTL,
    ) -> Lease | None:
        """Extend an existing lease's TTL."""
        rkey = self._resource_key(resource_id)
        raw = await self._client.hget(rkey, holder_id)
        if raw is None:
            return None

        data = json.loads(raw if isinstance(raw, str) else raw.decode())
        now = time.monotonic()
        if float(data["expires_at"]) <= now:
            await self._client.hdel(rkey, holder_id)
            return None

        data["expires_at"] = now + ttl
        await self._client.hset(rkey, holder_id, json.dumps(data))
        await self._client.expire(rkey, int(ttl) + 5)

        self._extend_count += 1
        return self._parse_lease(data, resource_id)

    async def leases_for_resource(self, resource_id: str) -> list[Lease]:
        """Return all active leases for a resource."""
        rkey = self._resource_key(resource_id)
        all_holders = await self._client.hgetall(rkey)
        now = time.monotonic()
        result: list[Lease] = []
        for _hid, raw in all_holders.items():
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            if float(data["expires_at"]) > now:
                result.append(self._parse_lease(data, resource_id))
        return result

    async def stats(self) -> dict[str, Any]:
        """Return operational statistics."""
        return {
            "acquire_count": self._acquire_count,
            "revoke_count": self._revoke_count,
            "extend_count": self._extend_count,
            "timeout_count": self._timeout_count,
            "callback_error_count": self._callback_error_count,
            "zone_id": self._zone_id,
        }

    async def force_revoke(self, resource_id: str) -> list[Lease]:
        """Force-revoke all holders without invoking callbacks."""
        rkey = self._resource_key(resource_id)
        all_holders = await self._client.hgetall(rkey)
        revoked: list[Lease] = []
        for _hid, raw in all_holders.items():
            data = json.loads(raw if isinstance(raw, str) else raw.decode())
            revoked.append(self._parse_lease(data, resource_id))
        if revoked:
            await self._client.delete(rkey)
            self._revoke_count += len(revoked)
        return revoked

    async def close(self) -> None:
        """No-op for distributed manager (no background tasks)."""
        pass

    # -- callback registration ------------------------------------------------

    def register_revocation_callback(
        self,
        callback_id: str,
        callback: RevocationCallback,
    ) -> None:
        """Register an async callback invoked on lease revocation."""
        for cid, _ in self._callbacks:
            if cid == callback_id:
                return
        self._callbacks.append((callback_id, callback))

    def unregister_revocation_callback(self, callback_id: str) -> bool:
        """Remove a previously registered callback."""
        for i, (cid, _) in enumerate(self._callbacks):
            if cid == callback_id:
                self._callbacks.pop(i)
                return True
        return False

    # -- internal helpers -----------------------------------------------------

    @staticmethod
    def _parse_lease(data: dict[str, Any], resource_id: str) -> Lease:
        """Parse a Lease from a Redis hash value."""
        return Lease(
            resource_id=data.get("resource_id", resource_id),
            holder_id=data["holder_id"],
            state=LeaseState(data["state"]),
            generation=int(data["generation"]),
            granted_at=float(data["granted_at"]),
            expires_at=float(data["expires_at"]),
        )

    async def _invoke_callbacks(self, leases: list[Lease], reason: str) -> None:
        """Invoke all registered callbacks concurrently with per-callback timeout."""
        if not self._callbacks or not leases:
            return

        async def _safe_invoke(cb_id: str, cb: RevocationCallback, lease: Lease) -> None:
            try:
                await asyncio.wait_for(cb(lease, reason), timeout=self._callback_timeout)
            except TimeoutError:
                self._callback_error_count += 1
                logger.warning(
                    "[DistributedLease] Callback %s timed out for %s:%s",
                    cb_id,
                    lease.resource_id,
                    lease.holder_id,
                )
            except Exception:
                self._callback_error_count += 1
                logger.warning(
                    "[DistributedLease] Callback %s failed for %s:%s",
                    cb_id,
                    lease.resource_id,
                    lease.holder_id,
                    exc_info=True,
                )

        tasks = [
            _safe_invoke(cb_id, cb, lease) for cb_id, cb in self._callbacks for lease in leases
        ]
        await asyncio.gather(*tasks)
