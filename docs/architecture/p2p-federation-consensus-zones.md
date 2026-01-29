# P2P Federation and Consensus Zones Design

**Status**: Proposed
**Author**: Nexus Team
**Created**: 2025-01-29
**Target Block**: Block 3+ (after P2P Foundation)

## Overview

This document describes the architecture for multi-region P2P federation in Nexus, including the consensus zone mechanism for configurable consistency levels. This design enables deployment across geographically distributed data centers (e.g., China, US, EU) while maintaining appropriate consistency guarantees.

## Problem Statement

### Current Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Current: Client-Server Model                               │
│                                                             │
│  ┌──────────────┐         ┌──────────────┐                 │
│  │   Server     │ ←────── │   Client     │                 │
│  │ NexusFS+RPC  │         │RemoteNexusFS │                 │
│  │ (authority)  │         │ (no storage) │                 │
│  └──────────────┘         └──────────────┘                 │
│                                                             │
│  - Single metadata authority                                │
│  - Clients cannot contribute files                          │
│  - All traffic through one server                           │
└─────────────────────────────────────────────────────────────┘
```

### Target Use Case: Multi-DC Deployment

```
China DC              US DC                EU DC
├── Agents            ├── Agents           ├── Agents
├── Nexus Box         ├── Nexus Box        ├── Nexus Box
├── Local files       ├── Local files      ├── Local files
└─────────────────────┴────────────────────┴──────────────
                 ↓ Shared View ↓
        Agent in any DC can see all files
```

### Challenges

1. **Latency**: Cross-region round-trips (CN-US: 150-300ms)
2. **Availability**: If one DC disconnects, should the system block?
3. **Consistency**: File systems expect strong consistency for metadata
4. **Locks**: Eventually consistent locks can cause data corruption

## Architecture

### Phase 1: P2P Foundation

Before implementing consensus zones, we need basic P2P infrastructure.

#### 1.1 Box Registry

Stores information about all boxes in a federation group (tenant).

```python
@dataclass
class BoxInfo:
    """Information about a Nexus box in the federation."""
    box_id: str                    # Unique identifier: "nexus-cn-01"
    endpoint: str                  # RPC endpoint: "http://cn.nexus.io:2026"
    region: str                    # Geographic region: "cn-shanghai"
    owned_path_prefixes: list[str] # Paths this box is authority for: ["/cn/*"]
    status: str                    # "healthy", "degraded", "offline"
    last_heartbeat: datetime       # Last health check timestamp
    capabilities: list[str]        # ["read", "write", "compute"]
```

**Storage**: Redis/Dragonfly (ephemeral, good for health checks)

```
Key: nexus:boxes:{tenant_id}:{box_id}
Value: JSON(BoxInfo)
TTL: 60s (requires heartbeat renewal)
```

#### 1.2 Path Ownership

Maps path prefixes to authoritative boxes.

```python
@dataclass
class PathOwnership:
    """Defines which box owns a path prefix."""
    path_prefix: str    # "/cn/*", "/us/data/*"
    owner_box_id: str   # "nexus-cn-01"
    fallback_box_id: str | None  # For failover
```

**Storage**: Redis/Dragonfly

```
Key: nexus:path_owners:{tenant_id}
Value: Hash {path_prefix → box_id}
```

#### 1.3 Cross-Box RPC Client

Lightweight internal RPC for box-to-box communication.

```python
class InternalRPCClient:
    """Box-to-box RPC client for federation."""

    def __init__(self, endpoint: str, internal_token: str):
        self.endpoint = endpoint
        self.token = internal_token  # Shared secret for internal auth

    async def read(self, path: str) -> bytes:
        """Forward read request to another box."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.endpoint}/api/internal/read",
                json={"path": path},
                headers={"X-Internal-Token": self.token}
            )
            return resp.content

    async def write(self, path: str, content: bytes) -> dict:
        """Forward write request to another box."""
        ...
```

#### 1.4 Federated Path Router

Extends PathRouter to support cross-box forwarding.

```python
class FederatedPathRouter(PathRouter):
    """Path router with cross-box federation support."""

    def __init__(self, box_registry: BoxRegistry, local_box_id: str):
        super().__init__()
        self.box_registry = box_registry
        self.local_box_id = local_box_id
        self._rpc_clients: dict[str, InternalRPCClient] = {}

    def route(self, path: str, tenant_id: str) -> RouteResult:
        """Route path to appropriate box."""
        owner_box_id = self._get_owner_box(path, tenant_id)

        if owner_box_id == self.local_box_id:
            # Local path - use normal routing
            return super().route(path)
        else:
            # Remote path - forward to owner box
            return RemoteRouteResult(
                box_id=owner_box_id,
                client=self._get_rpc_client(owner_box_id)
            )

    def _get_owner_box(self, path: str, tenant_id: str) -> str:
        """Find owner box using longest prefix match."""
        ownership = self.box_registry.get_path_ownership(tenant_id)

        # Sort by prefix length (descending) for longest match
        for prefix in sorted(ownership.keys(), key=len, reverse=True):
            if self._matches_prefix(path, prefix):
                return ownership[prefix]

        # Default to local box if no match
        return self.local_box_id
```

### Phase 2: Consensus Zones

Build on P2P foundation to add configurable consistency.

#### 2.1 Consistency Modes

```python
class ConsistencyMode(Enum):
    """Consistency level for a consensus zone."""

    STRONG = "strong"
    # - Single authority (owner box)
    # - All reads/writes go to authority
    # - Cross-region reads forward to authority
    # - Linearizable guarantees

    EVENTUAL = "eventual"
    # - Async replication to other boxes
    # - Local reads (may be stale)
    # - Writes to authority, async propagation
    # - No cross-region latency for reads

    QUORUM = "quorum"
    # - Requires N/2+1 acknowledgment
    # - For critical global data
    # - Higher latency but fault tolerant
```

#### 2.2 Consensus Zone Configuration

```python
@dataclass
class ConsensusZoneConfig:
    """Configuration for a consensus zone."""
    path_pattern: str           # "/cn/*", "/global/locks/*"
    mode: ConsistencyMode       # STRONG, EVENTUAL, QUORUM
    authority_box: str | None   # For STRONG mode
    replicas: list[str] | None  # For EVENTUAL mode
    quorum_size: int | None     # For QUORUM mode (e.g., 2 of 3)

    # Advanced options
    read_preference: str = "local"  # "local", "authority", "nearest"
    write_concern: str = "authority"  # "authority", "majority", "all"
    stale_read_max_age: float = 5.0  # Max staleness for EVENTUAL reads (seconds)
```

#### 2.3 Zone Registry

```python
class ConsensusZoneRegistry:
    """Registry for consensus zone configurations."""

    def __init__(self, redis_client: DragonflyClient):
        self.redis = redis_client
        self._cache: dict[str, ConsensusZoneConfig] = {}

    async def register_zone(
        self,
        tenant_id: str,
        config: ConsensusZoneConfig
    ) -> None:
        """Register a consensus zone."""
        key = f"nexus:zones:{tenant_id}"
        await self.redis.client.hset(
            key,
            config.path_pattern,
            config.to_json()
        )

    async def get_zone(
        self,
        tenant_id: str,
        path: str
    ) -> ConsensusZoneConfig | None:
        """Get zone config using longest prefix match."""
        key = f"nexus:zones:{tenant_id}"
        all_zones = await self.redis.client.hgetall(key)

        # Longest prefix match
        matched_zone = None
        matched_len = 0

        for pattern, config_json in all_zones.items():
            if self._matches_pattern(path, pattern):
                if len(pattern) > matched_len:
                    matched_zone = ConsensusZoneConfig.from_json(config_json)
                    matched_len = len(pattern)

        return matched_zone
```

#### 2.4 Zone-Aware Operations

```python
class ZoneAwareNexusFS(NexusFilesystem):
    """NexusFS with consensus zone awareness."""

    async def read(self, path: str, context: OperationContext) -> bytes:
        zone = await self.zone_registry.get_zone(context.tenant_id, path)

        if zone is None or zone.mode == ConsistencyMode.STRONG:
            # Strong: always read from authority
            return await self._read_from_authority(path, context)

        elif zone.mode == ConsistencyMode.EVENTUAL:
            # Eventual: try local first, fallback to authority
            local_result = await self._read_local(path, context)
            if local_result is not None:
                return local_result
            return await self._read_from_authority(path, context)

        elif zone.mode == ConsistencyMode.QUORUM:
            # Quorum: read from majority
            return await self._read_quorum(path, context, zone.quorum_size)

    async def write(self, path: str, content: bytes, context: OperationContext) -> dict:
        zone = await self.zone_registry.get_zone(context.tenant_id, path)

        if zone is None or zone.mode == ConsistencyMode.STRONG:
            # Strong: write to authority only
            return await self._write_to_authority(path, content, context)

        elif zone.mode == ConsistencyMode.EVENTUAL:
            # Eventual: write to authority, async replicate
            result = await self._write_to_authority(path, content, context)
            asyncio.create_task(self._async_replicate(path, content, zone.replicas))
            return result

        elif zone.mode == ConsistencyMode.QUORUM:
            # Quorum: write to majority before returning
            return await self._write_quorum(path, content, context, zone.quorum_size)
```

### Phase 3: Lock Consistency

Locks require special handling for correctness.

#### 3.1 Zone-Aware Distributed Lock

```python
class ZoneAwareLockManager:
    """Lock manager that respects consensus zones."""

    async def acquire(
        self,
        tenant_id: str,
        path: str,
        timeout: float = 30.0,
        ttl: float = 30.0
    ) -> str | None:
        zone = await self.zone_registry.get_zone(tenant_id, path)

        if zone is None or zone.mode in (ConsistencyMode.STRONG, ConsistencyMode.QUORUM):
            # Strong/Quorum: lock must go to authority or quorum
            authority = await self.box_registry.get_authority(tenant_id, path)
            if authority == self.local_box_id:
                return await self._local_acquire(tenant_id, path, timeout, ttl)
            else:
                return await self._remote_acquire(authority, tenant_id, path, timeout, ttl)

        elif zone.mode == ConsistencyMode.EVENTUAL:
            # EVENTUAL mode locks are dangerous!
            # Option 1: Upgrade to STRONG for lock operations
            # Option 2: Use fencing tokens
            # Option 3: Reject lock requests in EVENTUAL zones
            raise ConsistencyError(
                f"Cannot acquire lock in EVENTUAL consistency zone: {path}. "
                "Use STRONG or QUORUM consistency for paths that require locking."
            )
```

#### 3.2 Recommended Lock Zones

```python
# Default zone configuration for safe locking
DEFAULT_LOCK_ZONES = [
    ConsensusZoneConfig(
        path_pattern="/*/locks/*",
        mode=ConsistencyMode.STRONG,
        # Locks always use strong consistency
    ),
    ConsensusZoneConfig(
        path_pattern="/global/*",
        mode=ConsistencyMode.QUORUM,
        quorum_size=2,  # 2 of 3 DCs must agree
    ),
]
```

## Example: Multi-DC Configuration

### Deployment Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│  Tenant: "acme_corp"                                                │
│                                                                     │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐            │
│  │  China DC   │    │   US DC     │    │   EU DC     │            │
│  │ nexus-cn-01 │←──→│ nexus-us-01 │←──→│ nexus-eu-01 │            │
│  │ /cn/*       │    │ /us/*       │    │ /eu/*       │            │
│  └─────────────┘    └─────────────┘    └─────────────┘            │
│         ↑                  ↑                  ↑                    │
│         └──────────────────┼──────────────────┘                    │
│                     Shared Redis                                   │
│                  (Events, Locks, Registry)                         │
└─────────────────────────────────────────────────────────────────────┘
```

### Zone Configuration

```python
# Example configuration for acme_corp tenant
zones = [
    # Regional data - each DC is authority for its region
    ConsensusZoneConfig(
        path_pattern="/cn/*",
        mode=ConsistencyMode.STRONG,
        authority_box="nexus-cn-01",
    ),
    ConsensusZoneConfig(
        path_pattern="/us/*",
        mode=ConsistencyMode.STRONG,
        authority_box="nexus-us-01",
    ),
    ConsensusZoneConfig(
        path_pattern="/eu/*",
        mode=ConsistencyMode.STRONG,
        authority_box="nexus-eu-01",
    ),

    # Shared data with eventual consistency (fast reads)
    ConsensusZoneConfig(
        path_pattern="/shared/cache/*",
        mode=ConsistencyMode.EVENTUAL,
        authority_box="nexus-us-01",  # US is primary
        replicas=["nexus-cn-01", "nexus-eu-01"],
        stale_read_max_age=10.0,
    ),

    # Critical global data with quorum
    ConsensusZoneConfig(
        path_pattern="/global/config/*",
        mode=ConsistencyMode.QUORUM,
        quorum_size=2,  # 2 of 3 must agree
    ),

    # Locks always strong
    ConsensusZoneConfig(
        path_pattern="/*/locks/*",
        mode=ConsistencyMode.STRONG,
    ),
]
```

### Operation Examples

```python
# Agent in China writes to local path - FAST
await nx.write("/cn/agent-output/result.json", data)
# → Direct write to nexus-cn-01 (local)
# → Latency: <10ms

# Agent in China reads US data - SLOWER
content = await nx.read("/us/shared/model.bin")
# → Forward to nexus-us-01 (cross-Pacific)
# → Latency: 150-200ms

# Agent in China reads cached data - FAST (may be stale)
content = await nx.read("/shared/cache/config.json")
# → Read from local replica (nexus-cn-01)
# → Latency: <10ms
# → May be up to 10s stale

# Lock for meeting floor control - STRONG
lock_id = await nx.lock("/cn/meeting/floor")
# → Acquired on nexus-cn-01 (authority for /cn/*)
# → Strong consistency guaranteed
```

## Implementation Roadmap

### Block N: P2P Foundation (Current Focus)

| Component | Effort | Priority |
|-----------|--------|----------|
| BoxRegistry | ~100 lines | P0 |
| PathOwnership | ~50 lines | P0 |
| InternalRPCClient | ~100 lines | P0 |
| FederatedPathRouter | ~150 lines | P0 |
| **Total** | **~400 lines** | |

**Deliverables:**
- Boxes can discover each other
- Cross-box read/write works
- Compatible with existing client-server mode

### Block N+1: Consensus Zones

| Component | Effort | Priority |
|-----------|--------|----------|
| ConsensusZoneConfig | ~50 lines | P0 |
| ConsensusZoneRegistry | ~100 lines | P0 |
| ZoneAwareNexusFS | ~200 lines | P0 |
| ZoneAwareLockManager | ~100 lines | P0 |
| Async Replication | ~150 lines | P1 |
| Quorum Operations | ~200 lines | P1 |
| **Total** | **~800 lines** | |

**Deliverables:**
- Configurable consistency per path
- Strong/Eventual/Quorum modes
- Safe locking with zone awareness

### Block N+2: Production Hardening

| Component | Effort | Priority |
|-----------|--------|----------|
| Failover handling | ~200 lines | P0 |
| Health monitoring | ~150 lines | P0 |
| Metrics & observability | ~100 lines | P1 |
| Admin CLI for zones | ~100 lines | P2 |

## Compatibility

### With Existing Client-Server Mode

```
Federation mode does NOT break client-server:

┌─────────────────────────────────────────────────────────────┐
│  P2P Federation Layer                                       │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐                     │
│  │ Box CN  │←→│ Box US  │←→│ Box EU  │                     │
│  └────┬────┘  └────┬────┘  └────┬────┘                     │
└───────┼───────────┼───────────┼─────────────────────────────┘
        │           │           │
   ┌────┴────┐ ┌────┴────┐ ┌────┴────┐
   │Client 1 │ │Client 2 │ │Client 3 │  ← Legacy clients
   │(legacy) │ │(legacy) │ │(legacy) │    still work!
   └─────────┘ └─────────┘ └─────────┘

Clients connect to any box, routing handles the rest.
```

### With LocalConnectorBackend

LocalConnectorBackend works naturally in federation:

```python
# Box CN configuration
LocalConnectorBackend(
    mount_path="/cn/local-data",
    physical_root="C:\\projects\\data"
)

# Other boxes access via federation routing
# Box US: nx.read("/cn/local-data/file.txt")
#   → Forwards to Box CN
#   → Box CN uses LocalConnectorBackend
#   → Returns content to Box US
```

## Open Questions

1. **Redis Multi-Region**: Should we use Redis Cluster, Dragonfly Multi-Master, or separate Redis per region?

2. **Conflict Resolution**: For EVENTUAL mode, how do we handle write conflicts?
   - Last-writer-wins (LWW)?
   - Vector clocks?
   - Application-level merge?

3. **Partition Handling**: When a region becomes unreachable:
   - Block operations to that region's paths?
   - Allow stale reads?
   - Automatic failover to replica?

4. **Namespace Integration**: Should consensus zones be integrated with the existing namespace concept, or remain separate?

## References

- [Google Spanner Paper](https://research.google/pubs/pub39966/)
- [Zanzibar Paper](https://research.google/pubs/pub48190/)
- [Redis Cluster Specification](https://redis.io/docs/reference/cluster-spec/)
- [Dragonfly Multi-Master](https://www.dragonflydb.io/)
