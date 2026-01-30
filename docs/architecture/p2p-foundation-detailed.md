# P2P Foundation - Detailed Design

**Status**: In Design
**Parent Doc**: [p2p-federation-consensus-zones.md](./p2p-federation-consensus-zones.md)
**Block**: 2

## Overview

This document provides detailed implementation design for the P2P Foundation components that enable multi-box federation in Nexus.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  P2P Foundation Architecture                                            │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │  NexusFilesystem                                                 │   │
│  │       │                                                          │   │
│  │       ▼                                                          │   │
│  │  FederatedPathRouter ──────────────────────────────────────┐    │   │
│  │       │                                                     │    │   │
│  │       ├── Local? ──► PathRouter (existing)                  │    │   │
│  │       │                                                     │    │   │
│  │       └── Remote? ─► InternalRPCClient ──► Remote Box      │    │   │
│  │                            │                                │    │   │
│  │                            ▼                                │    │   │
│  │                      BoxRegistry ◄── PathOwnership          │    │   │
│  │                            │                                │    │   │
│  │                            ▼                                │    │   │
│  │                      Dragonfly (Redis)                      │    │   │
│  └─────────────────────────────────────────────────────────────┴───┘   │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 1. BoxRegistry

### 1.1 Purpose

Maintains a registry of all Nexus boxes in a federation, supporting:
- Box discovery and health tracking
- Heartbeat-based liveness detection
- Capability advertisement

### 1.2 Data Model

```python
# src/nexus/federation/box_registry.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BoxStatus(Enum):
    """Health status of a Nexus box."""
    HEALTHY = "healthy"      # All systems operational
    DEGRADED = "degraded"    # Partial functionality
    OFFLINE = "offline"      # No heartbeat received
    STARTING = "starting"    # Box is initializing


class BoxCapability(Enum):
    """Capabilities a box can advertise."""
    READ = "read"            # Can serve read requests
    WRITE = "write"          # Can serve write requests
    COMPUTE = "compute"      # Can run sandboxed code
    SEARCH = "search"        # Has search/embedding capability
    RAFT_VOTER = "raft_voter"  # Can participate in Raft voting


@dataclass
class BoxInfo:
    """Information about a Nexus box in the federation."""

    # Identity
    box_id: str                          # Unique identifier: "nexus-cn-01"
    tenant_id: str                       # Tenant this box belongs to

    # Network
    endpoint: str                        # RPC endpoint: "http://cn.nexus.io:2026"
    internal_endpoint: str | None = None # Internal network endpoint (if different)

    # Location
    region: str = "default"              # Geographic region: "cn-shanghai"
    zone: str | None = None              # Availability zone: "cn-shanghai-a"

    # Ownership
    owned_path_prefixes: list[str] = field(default_factory=list)  # ["/cn/*"]

    # Status
    status: BoxStatus = BoxStatus.STARTING
    last_heartbeat: datetime | None = None
    version: str | None = None           # Nexus version: "0.9.0"

    # Capabilities
    capabilities: list[BoxCapability] = field(default_factory=list)

    # Metadata
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_healthy(self) -> bool:
        """Check if box is healthy and can serve requests."""
        return self.status == BoxStatus.HEALTHY

    def can_read(self) -> bool:
        return BoxCapability.READ in self.capabilities and self.is_healthy()

    def can_write(self) -> bool:
        return BoxCapability.WRITE in self.capabilities and self.is_healthy()

    def to_dict(self) -> dict[str, Any]:
        """Serialize for Redis storage."""
        return {
            "box_id": self.box_id,
            "tenant_id": self.tenant_id,
            "endpoint": self.endpoint,
            "internal_endpoint": self.internal_endpoint,
            "region": self.region,
            "zone": self.zone,
            "owned_path_prefixes": self.owned_path_prefixes,
            "status": self.status.value,
            "last_heartbeat": self.last_heartbeat.isoformat() if self.last_heartbeat else None,
            "version": self.version,
            "capabilities": [c.value for c in self.capabilities],
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BoxInfo":
        """Deserialize from Redis storage."""
        return cls(
            box_id=data["box_id"],
            tenant_id=data["tenant_id"],
            endpoint=data["endpoint"],
            internal_endpoint=data.get("internal_endpoint"),
            region=data.get("region", "default"),
            zone=data.get("zone"),
            owned_path_prefixes=data.get("owned_path_prefixes", []),
            status=BoxStatus(data.get("status", "starting")),
            last_heartbeat=datetime.fromisoformat(data["last_heartbeat"]) if data.get("last_heartbeat") else None,
            version=data.get("version"),
            capabilities=[BoxCapability(c) for c in data.get("capabilities", [])],
            metadata=data.get("metadata", {}),
        )
```

### 1.3 Registry Implementation

```python
# src/nexus/federation/box_registry.py (continued)

import asyncio
import json
import logging
from datetime import datetime, timedelta

from nexus.core.cache.dragonfly import DragonflyClient

logger = logging.getLogger(__name__)


class BoxRegistry:
    """Registry for discovering and tracking Nexus boxes in a federation.

    Uses Dragonfly/Redis for distributed state with TTL-based health tracking.

    Redis Keys:
        nexus:boxes:{tenant_id}:{box_id} -> BoxInfo JSON (TTL: 60s)
        nexus:boxes:{tenant_id}:_index   -> Set of box_ids

    Example:
        >>> registry = BoxRegistry(dragonfly_client, local_box_id="nexus-cn-01")
        >>> await registry.register(box_info)
        >>> await registry.start_heartbeat()  # Background task
        >>>
        >>> # Discover other boxes
        >>> boxes = await registry.list_boxes("tenant-123")
        >>> healthy = [b for b in boxes if b.is_healthy()]
    """

    HEARTBEAT_INTERVAL = 20  # seconds
    BOX_TTL = 60  # seconds - box considered offline if no heartbeat

    def __init__(
        self,
        dragonfly: DragonflyClient,
        local_box_id: str,
        local_box_info: BoxInfo | None = None,
    ):
        self.dragonfly = dragonfly
        self.local_box_id = local_box_id
        self.local_box_info = local_box_info
        self._heartbeat_task: asyncio.Task | None = None
        self._cache: dict[str, BoxInfo] = {}  # Local cache for performance
        self._cache_ttl = 5  # seconds
        self._cache_updated: datetime | None = None

    # =========================================================================
    # Registration
    # =========================================================================

    async def register(self, box_info: BoxInfo) -> None:
        """Register a box in the federation.

        Args:
            box_info: Box information to register
        """
        key = self._box_key(box_info.tenant_id, box_info.box_id)
        index_key = self._index_key(box_info.tenant_id)

        # Update heartbeat timestamp
        box_info.last_heartbeat = datetime.utcnow()
        box_info.status = BoxStatus.HEALTHY

        # Store with TTL
        await self.dragonfly.client.setex(
            key,
            self.BOX_TTL,
            json.dumps(box_info.to_dict()),
        )

        # Add to index
        await self.dragonfly.client.sadd(index_key, box_info.box_id)

        logger.info(f"Registered box: {box_info.box_id} in tenant {box_info.tenant_id}")

    async def unregister(self, tenant_id: str, box_id: str) -> None:
        """Unregister a box from the federation."""
        key = self._box_key(tenant_id, box_id)
        index_key = self._index_key(tenant_id)

        await self.dragonfly.client.delete(key)
        await self.dragonfly.client.srem(index_key, box_id)

        logger.info(f"Unregistered box: {box_id}")

    # =========================================================================
    # Discovery
    # =========================================================================

    async def get_box(self, tenant_id: str, box_id: str) -> BoxInfo | None:
        """Get info for a specific box."""
        key = self._box_key(tenant_id, box_id)
        data = await self.dragonfly.client.get(key)

        if data is None:
            return None

        return BoxInfo.from_dict(json.loads(data))

    async def list_boxes(
        self,
        tenant_id: str,
        *,
        healthy_only: bool = False,
        capability: BoxCapability | None = None,
    ) -> list[BoxInfo]:
        """List all boxes in a tenant's federation.

        Args:
            tenant_id: Tenant to list boxes for
            healthy_only: Only return healthy boxes
            capability: Filter by capability

        Returns:
            List of BoxInfo objects
        """
        index_key = self._index_key(tenant_id)
        box_ids = await self.dragonfly.client.smembers(index_key)

        boxes = []
        for box_id in box_ids:
            box = await self.get_box(tenant_id, box_id)
            if box is None:
                # Box expired, clean up index
                await self.dragonfly.client.srem(index_key, box_id)
                continue

            if healthy_only and not box.is_healthy():
                continue

            if capability and capability not in box.capabilities:
                continue

            boxes.append(box)

        return boxes

    async def find_box_for_path(
        self,
        tenant_id: str,
        path: str,
    ) -> BoxInfo | None:
        """Find the authoritative box for a path.

        Uses longest prefix match on owned_path_prefixes.
        """
        boxes = await self.list_boxes(tenant_id, healthy_only=True)

        best_match: BoxInfo | None = None
        best_match_len = 0

        for box in boxes:
            for prefix in box.owned_path_prefixes:
                if self._matches_prefix(path, prefix) and len(prefix) > best_match_len:
                    best_match = box
                    best_match_len = len(prefix)

        return best_match

    # =========================================================================
    # Heartbeat
    # =========================================================================

    async def start_heartbeat(self) -> None:
        """Start background heartbeat task for local box."""
        if self.local_box_info is None:
            raise ValueError("Cannot start heartbeat without local_box_info")

        if self._heartbeat_task is not None:
            return  # Already running

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(f"Started heartbeat for box: {self.local_box_id}")

    async def stop_heartbeat(self) -> None:
        """Stop background heartbeat task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

    async def _heartbeat_loop(self) -> None:
        """Background loop to send heartbeats."""
        while True:
            try:
                await self.register(self.local_box_info)
            except Exception as e:
                logger.error(f"Heartbeat failed: {e}")

            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    # =========================================================================
    # Helpers
    # =========================================================================

    def _box_key(self, tenant_id: str, box_id: str) -> str:
        return f"nexus:boxes:{tenant_id}:{box_id}"

    def _index_key(self, tenant_id: str) -> str:
        return f"nexus:boxes:{tenant_id}:_index"

    def _matches_prefix(self, path: str, prefix: str) -> bool:
        """Check if path matches prefix pattern (supports * wildcard)."""
        if prefix.endswith("/*"):
            return path.startswith(prefix[:-2])
        return path == prefix or path.startswith(prefix + "/")
```

---

## 2. PathOwnership

### 2.1 Purpose

Maps path prefixes to their authoritative boxes. Simpler than BoxRegistry - focuses only on routing.

### 2.2 Implementation

```python
# src/nexus/federation/path_ownership.py

import json
import logging
from dataclasses import dataclass

from nexus.core.cache.dragonfly import DragonflyClient

logger = logging.getLogger(__name__)


@dataclass
class PathOwnershipEntry:
    """Defines ownership of a path prefix."""
    path_prefix: str          # "/cn/*", "/us/data/*"
    owner_box_id: str         # "nexus-cn-01"
    fallback_box_id: str | None = None  # For failover
    priority: int = 0         # Higher = more specific (auto-calculated from prefix length)


class PathOwnership:
    """Manages path prefix to box ownership mapping.

    Redis Key:
        nexus:path_owners:{tenant_id} -> Hash {prefix: JSON(entry)}

    Example:
        >>> ownership = PathOwnership(dragonfly)
        >>> await ownership.set_owner("tenant-1", "/cn/*", "nexus-cn-01")
        >>> owner = await ownership.get_owner("tenant-1", "/cn/data/file.txt")
        >>> assert owner == "nexus-cn-01"
    """

    def __init__(self, dragonfly: DragonflyClient):
        self.dragonfly = dragonfly
        self._cache: dict[str, dict[str, PathOwnershipEntry]] = {}

    async def set_owner(
        self,
        tenant_id: str,
        path_prefix: str,
        owner_box_id: str,
        fallback_box_id: str | None = None,
    ) -> None:
        """Set ownership for a path prefix."""
        key = self._key(tenant_id)
        entry = PathOwnershipEntry(
            path_prefix=path_prefix,
            owner_box_id=owner_box_id,
            fallback_box_id=fallback_box_id,
            priority=len(path_prefix),  # Longer = more specific
        )

        await self.dragonfly.client.hset(key, path_prefix, json.dumps({
            "owner_box_id": entry.owner_box_id,
            "fallback_box_id": entry.fallback_box_id,
            "priority": entry.priority,
        }))

        # Invalidate cache
        self._cache.pop(tenant_id, None)

        logger.info(f"Set path owner: {path_prefix} -> {owner_box_id}")

    async def remove_owner(self, tenant_id: str, path_prefix: str) -> None:
        """Remove ownership for a path prefix."""
        key = self._key(tenant_id)
        await self.dragonfly.client.hdel(key, path_prefix)
        self._cache.pop(tenant_id, None)

    async def get_owner(
        self,
        tenant_id: str,
        path: str,
        *,
        use_fallback: bool = False,
    ) -> str | None:
        """Get the owner box for a path using longest prefix match.

        Args:
            tenant_id: Tenant ID
            path: Full path to look up
            use_fallback: Return fallback box if primary is unavailable

        Returns:
            Box ID of the owner, or None if no owner configured
        """
        entries = await self._get_all_entries(tenant_id)

        best_match: PathOwnershipEntry | None = None

        for entry in entries.values():
            if self._matches_prefix(path, entry.path_prefix):
                if best_match is None or entry.priority > best_match.priority:
                    best_match = entry

        if best_match is None:
            return None

        if use_fallback and best_match.fallback_box_id:
            return best_match.fallback_box_id

        return best_match.owner_box_id

    async def get_all_prefixes(self, tenant_id: str) -> dict[str, str]:
        """Get all prefix -> owner mappings for a tenant."""
        entries = await self._get_all_entries(tenant_id)
        return {prefix: entry.owner_box_id for prefix, entry in entries.items()}

    async def _get_all_entries(self, tenant_id: str) -> dict[str, PathOwnershipEntry]:
        """Get all entries with caching."""
        if tenant_id in self._cache:
            return self._cache[tenant_id]

        key = self._key(tenant_id)
        raw_entries = await self.dragonfly.client.hgetall(key)

        entries = {}
        for prefix, data_str in raw_entries.items():
            data = json.loads(data_str)
            entries[prefix] = PathOwnershipEntry(
                path_prefix=prefix,
                owner_box_id=data["owner_box_id"],
                fallback_box_id=data.get("fallback_box_id"),
                priority=data.get("priority", len(prefix)),
            )

        self._cache[tenant_id] = entries
        return entries

    def _key(self, tenant_id: str) -> str:
        return f"nexus:path_owners:{tenant_id}"

    def _matches_prefix(self, path: str, prefix: str) -> bool:
        """Check if path matches prefix pattern."""
        if prefix.endswith("/*"):
            return path.startswith(prefix[:-2])
        return path == prefix or path.startswith(prefix + "/")
```

---

## 3. InternalRPCClient

### 3.1 Purpose

Handles box-to-box communication for forwarding requests to remote boxes.

### 3.2 Implementation

```python
# src/nexus/federation/internal_rpc.py

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from nexus.core.exceptions import FederationError

logger = logging.getLogger(__name__)


@dataclass
class RPCResponse:
    """Response from internal RPC call."""
    success: bool
    data: Any = None
    error: str | None = None


class InternalRPCClient:
    """Client for box-to-box RPC communication.

    Uses HTTP/JSON for simplicity. Can be upgraded to gRPC for performance.

    Security:
    - Uses internal token for authentication (shared secret between boxes)
    - Should only be accessible on internal network

    Example:
        >>> client = InternalRPCClient(
        ...     endpoint="http://nexus-cn-01:2026",
        ...     internal_token="shared-secret-token"
        ... )
        >>> content = await client.read("/cn/data/file.txt", tenant_id="t1")
    """

    DEFAULT_TIMEOUT = 30.0  # seconds

    def __init__(
        self,
        endpoint: str,
        internal_token: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.internal_token = internal_token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={
                    "X-Internal-Token": self.internal_token,
                    "X-Forwarded-By": "nexus-federation",
                },
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # =========================================================================
    # File Operations
    # =========================================================================

    async def read(
        self,
        path: str,
        tenant_id: str,
        user_id: str | None = None,
    ) -> bytes:
        """Read file content from remote box.

        Args:
            path: Virtual path to read
            tenant_id: Tenant ID for authorization
            user_id: User ID for authorization

        Returns:
            File content as bytes

        Raises:
            FederationError: If read fails
        """
        client = await self._get_client()

        try:
            resp = await client.post(
                f"{self.endpoint}/api/internal/read",
                json={
                    "path": path,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            resp.raise_for_status()
            return resp.content

        except httpx.HTTPStatusError as e:
            raise FederationError(f"Remote read failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise FederationError(f"Remote read failed: {e}") from e

    async def write(
        self,
        path: str,
        content: bytes,
        tenant_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """Write file content to remote box.

        Args:
            path: Virtual path to write
            content: File content
            tenant_id: Tenant ID for authorization
            user_id: User ID for authorization

        Returns:
            Write result (hash, size, etc.)

        Raises:
            FederationError: If write fails
        """
        client = await self._get_client()

        try:
            resp = await client.post(
                f"{self.endpoint}/api/internal/write",
                json={
                    "path": path,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
                content=content,
                headers={"Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            raise FederationError(f"Remote write failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise FederationError(f"Remote write failed: {e}") from e

    async def delete(
        self,
        path: str,
        tenant_id: str,
        user_id: str | None = None,
    ) -> bool:
        """Delete file on remote box."""
        client = await self._get_client()

        try:
            resp = await client.post(
                f"{self.endpoint}/api/internal/delete",
                json={
                    "path": path,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            resp.raise_for_status()
            return True

        except httpx.HTTPStatusError as e:
            raise FederationError(f"Remote delete failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise FederationError(f"Remote delete failed: {e}") from e

    async def list_dir(
        self,
        path: str,
        tenant_id: str,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List directory on remote box."""
        client = await self._get_client()

        try:
            resp = await client.post(
                f"{self.endpoint}/api/internal/list",
                json={
                    "path": path,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                },
            )
            resp.raise_for_status()
            return resp.json()

        except httpx.HTTPStatusError as e:
            raise FederationError(f"Remote list failed: {e.response.status_code}") from e
        except httpx.RequestError as e:
            raise FederationError(f"Remote list failed: {e}") from e

    # =========================================================================
    # Health & Status
    # =========================================================================

    async def health_check(self) -> bool:
        """Check if remote box is healthy."""
        client = await self._get_client()

        try:
            resp = await client.get(f"{self.endpoint}/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def get_status(self) -> dict[str, Any]:
        """Get detailed status from remote box."""
        client = await self._get_client()

        resp = await client.get(f"{self.endpoint}/api/internal/status")
        resp.raise_for_status()
        return resp.json()


class InternalRPCClientPool:
    """Pool of RPC clients for multiple remote boxes.

    Manages connections and provides client reuse.

    Example:
        >>> pool = InternalRPCClientPool(internal_token="secret")
        >>> client = pool.get_client("http://nexus-cn-01:2026")
        >>> await client.read("/path", "tenant-1")
    """

    def __init__(self, internal_token: str):
        self.internal_token = internal_token
        self._clients: dict[str, InternalRPCClient] = {}

    def get_client(self, endpoint: str) -> InternalRPCClient:
        """Get or create a client for an endpoint."""
        if endpoint not in self._clients:
            self._clients[endpoint] = InternalRPCClient(
                endpoint=endpoint,
                internal_token=self.internal_token,
            )
        return self._clients[endpoint]

    async def close_all(self) -> None:
        """Close all clients."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
```

---

## 4. FederatedPathRouter

### 4.1 Purpose

Extends the existing PathRouter to support cross-box routing. When a path is owned by a remote box, it forwards the request via InternalRPCClient.

### 4.2 Implementation

```python
# src/nexus/federation/federated_router.py

import logging
from dataclasses import dataclass
from typing import Any

from nexus.core.path_router import PathRouter, RouteResult
from nexus.federation.box_registry import BoxRegistry, BoxInfo
from nexus.federation.path_ownership import PathOwnership
from nexus.federation.internal_rpc import InternalRPCClient, InternalRPCClientPool

logger = logging.getLogger(__name__)


@dataclass
class LocalRouteResult:
    """Route result for local paths."""
    is_local: bool = True
    mount_point: str | None = None
    backend_path: str | None = None


@dataclass
class RemoteRouteResult:
    """Route result for paths owned by remote boxes."""
    is_local: bool = False
    box_id: str
    box_info: BoxInfo
    client: InternalRPCClient


class FederatedPathRouter:
    """Path router with cross-box federation support.

    Routing logic:
    1. Check PathOwnership for explicit owner
    2. If owner is local box -> use normal PathRouter
    3. If owner is remote box -> forward via InternalRPCClient
    4. If no owner -> default to local box

    Example:
        >>> router = FederatedPathRouter(
        ...     local_router=PathRouter(),
        ...     box_registry=box_registry,
        ...     path_ownership=path_ownership,
        ...     local_box_id="nexus-cn-01",
        ...     internal_token="secret",
        ... )
        >>>
        >>> result = await router.route("/cn/data/file.txt", "tenant-1")
        >>> if result.is_local:
        ...     # Handle locally
        ...     content = await local_fs.read(result.backend_path)
        ... else:
        ...     # Forward to remote
        ...     content = await result.client.read("/cn/data/file.txt", "tenant-1")
    """

    def __init__(
        self,
        local_router: PathRouter,
        box_registry: BoxRegistry,
        path_ownership: PathOwnership,
        local_box_id: str,
        internal_token: str,
    ):
        self.local_router = local_router
        self.box_registry = box_registry
        self.path_ownership = path_ownership
        self.local_box_id = local_box_id
        self._rpc_pool = InternalRPCClientPool(internal_token)

    async def route(
        self,
        path: str,
        tenant_id: str,
    ) -> LocalRouteResult | RemoteRouteResult:
        """Route a path to the appropriate box.

        Args:
            path: Virtual path to route
            tenant_id: Tenant ID for ownership lookup

        Returns:
            LocalRouteResult if path is owned locally
            RemoteRouteResult if path is owned by a remote box
        """
        # 1. Find owner box
        owner_box_id = await self.path_ownership.get_owner(tenant_id, path)

        # 2. Default to local if no owner configured
        if owner_box_id is None:
            owner_box_id = self.local_box_id

        # 3. Check if local
        if owner_box_id == self.local_box_id:
            # Route locally using existing PathRouter
            local_result = self.local_router.route(path)
            return LocalRouteResult(
                is_local=True,
                mount_point=local_result.mount_point if hasattr(local_result, 'mount_point') else None,
                backend_path=local_result.backend_path if hasattr(local_result, 'backend_path') else path,
            )

        # 4. Route to remote box
        box_info = await self.box_registry.get_box(tenant_id, owner_box_id)

        if box_info is None:
            # Owner configured but box not found - try fallback
            fallback_id = await self.path_ownership.get_owner(
                tenant_id, path, use_fallback=True
            )
            if fallback_id and fallback_id != owner_box_id:
                box_info = await self.box_registry.get_box(tenant_id, fallback_id)
                owner_box_id = fallback_id

        if box_info is None:
            # No box available - fall back to local
            logger.warning(f"Owner box {owner_box_id} not found, falling back to local")
            return LocalRouteResult(is_local=True, backend_path=path)

        if not box_info.is_healthy():
            logger.warning(f"Owner box {owner_box_id} is unhealthy: {box_info.status}")
            # Could implement retry logic or fallback here

        # Get RPC client for remote box
        endpoint = box_info.internal_endpoint or box_info.endpoint
        client = self._rpc_pool.get_client(endpoint)

        return RemoteRouteResult(
            is_local=False,
            box_id=owner_box_id,
            box_info=box_info,
            client=client,
        )

    async def route_with_fallback(
        self,
        path: str,
        tenant_id: str,
        *,
        max_retries: int = 2,
    ) -> LocalRouteResult | RemoteRouteResult:
        """Route with automatic fallback on failure.

        If primary box is unavailable, tries fallback box.
        """
        result = await self.route(path, tenant_id)

        if result.is_local:
            return result

        # Check if remote box is healthy
        if not result.box_info.is_healthy():
            # Try fallback
            fallback_id = await self.path_ownership.get_owner(
                tenant_id, path, use_fallback=True
            )

            if fallback_id and fallback_id != result.box_id:
                fallback_info = await self.box_registry.get_box(tenant_id, fallback_id)
                if fallback_info and fallback_info.is_healthy():
                    endpoint = fallback_info.internal_endpoint or fallback_info.endpoint
                    return RemoteRouteResult(
                        is_local=False,
                        box_id=fallback_id,
                        box_info=fallback_info,
                        client=self._rpc_pool.get_client(endpoint),
                    )

        return result

    async def close(self) -> None:
        """Close all RPC clients."""
        await self._rpc_pool.close_all()
```

---

## 5. Integration with NexusFilesystem

### 5.1 Federation-Aware NexusFS

```python
# src/nexus/federation/federated_fs.py

from nexus.core.nexus_fs import NexusFilesystem
from nexus.federation.federated_router import FederatedPathRouter, LocalRouteResult


class FederatedNexusFS(NexusFilesystem):
    """NexusFilesystem with federation support.

    Transparently routes requests to local or remote boxes based on path ownership.
    """

    def __init__(
        self,
        federated_router: FederatedPathRouter,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.federated_router = federated_router

    async def read(
        self,
        path: str,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> bytes:
        """Read file, routing to appropriate box."""
        tenant_id = tenant_id or self._get_default_tenant()

        route = await self.federated_router.route(path, tenant_id)

        if route.is_local:
            # Use existing local read
            return await super().read(path, tenant_id=tenant_id, user_id=user_id)
        else:
            # Forward to remote box
            return await route.client.read(path, tenant_id, user_id)

    async def write(
        self,
        path: str,
        content: bytes,
        *,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> dict:
        """Write file, routing to appropriate box."""
        tenant_id = tenant_id or self._get_default_tenant()

        route = await self.federated_router.route(path, tenant_id)

        if route.is_local:
            return await super().write(path, content, tenant_id=tenant_id, user_id=user_id)
        else:
            return await route.client.write(path, content, tenant_id, user_id)

    # ... similar for delete, list, etc.
```

---

## 6. Server-Side: Internal API Endpoints

### 6.1 Internal RPC Handler

```python
# src/nexus/server/routes/internal.py

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/internal", tags=["internal"])


def verify_internal_token(x_internal_token: str = Header(...)) -> str:
    """Verify internal token for box-to-box auth."""
    expected = get_internal_token()  # From config
    if x_internal_token != expected:
        raise HTTPException(status_code=401, detail="Invalid internal token")
    return x_internal_token


class ReadRequest(BaseModel):
    path: str
    tenant_id: str
    user_id: str | None = None


@router.post("/read")
async def internal_read(
    request: ReadRequest,
    _token: str = Depends(verify_internal_token),
    nx: NexusFilesystem = Depends(get_nexus),
) -> bytes:
    """Handle forwarded read request from another box."""
    return await nx.read(
        request.path,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
    )


@router.post("/write")
async def internal_write(
    request: Request,
    path: str,
    tenant_id: str,
    user_id: str | None = None,
    _token: str = Depends(verify_internal_token),
    nx: NexusFilesystem = Depends(get_nexus),
) -> dict:
    """Handle forwarded write request from another box."""
    content = await request.body()
    return await nx.write(
        path,
        content,
        tenant_id=tenant_id,
        user_id=user_id,
    )


# ... similar for delete, list, etc.
```

---

## 7. Configuration

### 7.1 Environment Variables

```bash
# Box identity
NEXUS_BOX_ID=nexus-cn-01
NEXUS_BOX_REGION=cn-shanghai
NEXUS_BOX_ZONE=cn-shanghai-a

# Federation
NEXUS_FEDERATION_ENABLED=true
NEXUS_INTERNAL_TOKEN=shared-secret-for-box-communication

# Path ownership (comma-separated prefixes this box owns)
NEXUS_OWNED_PATHS=/cn/*,/shared/cache/*

# Dragonfly for registry
NEXUS_DRAGONFLY_URL=redis://dragonfly:6379
```

### 7.2 Config File

```yaml
# configs/federation.yaml

federation:
  enabled: true

  box:
    id: nexus-cn-01
    region: cn-shanghai
    zone: cn-shanghai-a
    capabilities:
      - read
      - write
      - search

  owned_paths:
    - /cn/*
    - /shared/cache/*

  # Internal auth
  internal_token: ${NEXUS_INTERNAL_TOKEN}

  # Discovery
  dragonfly_url: ${NEXUS_DRAGONFLY_URL}
```

---

## 8. Testing Strategy

### 8.1 Unit Tests

```python
# tests/unit/federation/test_box_registry.py

import pytest
from nexus.federation.box_registry import BoxRegistry, BoxInfo, BoxStatus


class TestBoxRegistry:
    async def test_register_and_get(self, mock_dragonfly):
        registry = BoxRegistry(mock_dragonfly, local_box_id="box-1")

        box_info = BoxInfo(
            box_id="box-1",
            tenant_id="tenant-1",
            endpoint="http://box-1:2026",
        )

        await registry.register(box_info)

        retrieved = await registry.get_box("tenant-1", "box-1")
        assert retrieved is not None
        assert retrieved.box_id == "box-1"
        assert retrieved.status == BoxStatus.HEALTHY


# tests/unit/federation/test_federated_router.py

class TestFederatedRouter:
    async def test_route_local(self, router):
        # Path owned by local box
        result = await router.route("/cn/data/file.txt", "tenant-1")
        assert result.is_local is True

    async def test_route_remote(self, router):
        # Path owned by remote box
        result = await router.route("/us/data/file.txt", "tenant-1")
        assert result.is_local is False
        assert result.box_id == "nexus-us-01"
```

### 8.2 Integration Tests (Docker)

```yaml
# docker-compose.test-p2p.yml

services:
  dragonfly:
    image: docker.dragonflydb.io/dragonflydb/dragonfly
    ports:
      - "6379:6379"

  box-1:
    build: .
    environment:
      NEXUS_BOX_ID: box-1
      NEXUS_OWNED_PATHS: /box1/*
      NEXUS_DRAGONFLY_URL: redis://dragonfly:6379
      NEXUS_INTERNAL_TOKEN: test-token
    depends_on:
      - dragonfly

  box-2:
    build: .
    environment:
      NEXUS_BOX_ID: box-2
      NEXUS_OWNED_PATHS: /box2/*
      NEXUS_DRAGONFLY_URL: redis://dragonfly:6379
      NEXUS_INTERNAL_TOKEN: test-token
    depends_on:
      - dragonfly

  test:
    build:
      context: .
      dockerfile: Dockerfile.test
    command: pytest tests/integration/test_federation.py -v
    depends_on:
      - box-1
      - box-2
```

```python
# tests/integration/test_federation.py

import pytest
import httpx


@pytest.mark.integration
class TestFederation:
    async def test_cross_box_read(self):
        """Test reading a file owned by another box."""
        async with httpx.AsyncClient() as client:
            # Write to box-2 via box-1
            resp = await client.post(
                "http://box-1:2026/api/write",
                json={"path": "/box2/test.txt"},
                content=b"hello from box-1",
            )
            assert resp.status_code == 200

            # Read from box-2 via box-1 (should forward)
            resp = await client.get(
                "http://box-1:2026/api/read",
                params={"path": "/box2/test.txt"},
            )
            assert resp.content == b"hello from box-1"
```

---

## 9. File Structure

```
src/nexus/federation/
├── __init__.py
├── box_registry.py      # BoxInfo, BoxRegistry
├── path_ownership.py    # PathOwnership
├── internal_rpc.py      # InternalRPCClient, InternalRPCClientPool
├── federated_router.py  # FederatedPathRouter
├── federated_fs.py      # FederatedNexusFS
└── config.py            # FederationConfig

src/nexus/server/routes/
└── internal.py          # Internal API endpoints

tests/unit/federation/
├── test_box_registry.py
├── test_path_ownership.py
├── test_internal_rpc.py
└── test_federated_router.py

tests/integration/
└── test_federation.py
```

---

## 10. Implementation Order

| Step | Component | Depends On | Effort |
|------|-----------|------------|--------|
| 1 | BoxInfo, BoxStatus, BoxCapability | - | ~50 lines |
| 2 | BoxRegistry | Dragonfly | ~150 lines |
| 3 | PathOwnership | Dragonfly | ~80 lines |
| 4 | InternalRPCClient | httpx | ~150 lines |
| 5 | FederatedPathRouter | 2, 3, 4 | ~120 lines |
| 6 | Internal API endpoints | FastAPI | ~80 lines |
| 7 | FederatedNexusFS | 5, NexusFS | ~100 lines |
| 8 | Unit tests | - | ~200 lines |
| 9 | Integration tests | Docker | ~100 lines |
| **Total** | | | **~1030 lines** |
