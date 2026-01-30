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
    # - ⚠️ Single point of failure

    STRONG_HA = "strong_ha"
    # - Raft-based consensus (leader + followers + witness)
    # - Automatic leader election on failure
    # - Linearizable guarantees WITH high availability
    # - Uses tikv/raft-rs for consensus
    # - Witness node: lightweight, vote-only (no data)

    EVENTUAL = "eventual"
    # - Async replication to other boxes
    # - Local reads (may be stale)
    # - Writes to authority, async propagation
    # - No cross-region latency for reads

    QUORUM = "quorum"
    # - Requires N/2+1 acknowledgment
    # - For critical global data
    # - Higher latency but fault tolerant
    # - Note: Consider STRONG_HA for better consistency guarantees
```

#### 2.2 Consensus Zone Configuration

```python
@dataclass
class ConsensusZoneConfig:
    """Configuration for a consensus zone."""
    path_pattern: str           # "/cn/*", "/global/locks/*"
    mode: ConsistencyMode       # STRONG, STRONG_HA, EVENTUAL, QUORUM
    authority_box: str | None   # For STRONG mode
    replicas: list[str] | None  # For EVENTUAL mode
    quorum_size: int | None     # For QUORUM mode (e.g., 2 of 3)

    # Raft configuration (for STRONG_HA mode)
    raft_group: RaftGroupConfig | None = None

    # Advanced options
    read_preference: str = "local"  # "local", "authority", "nearest", "leader"
    write_concern: str = "authority"  # "authority", "majority", "all"
    stale_read_max_age: float = 5.0  # Max staleness for EVENTUAL reads (seconds)


@dataclass
class RaftGroupConfig:
    """Configuration for a Raft consensus group."""
    group_id: str                    # Unique Raft group ID: "raft-cn-primary"
    members: list[RaftMember]        # Full members (vote + data)
    witnesses: list[RaftWitness]     # Witness nodes (vote only, no data)
    election_timeout_ms: int = 1000  # Leader election timeout
    heartbeat_interval_ms: int = 100 # Leader heartbeat interval


@dataclass
class RaftMember:
    """A full Raft member (stores data and votes)."""
    box_id: str           # "nexus-cn-01"
    endpoint: str         # "http://cn.nexus.io:2026"
    is_learner: bool = False  # Learners don't vote but receive data


@dataclass
class RaftWitness:
    """A lightweight Raft witness (votes but doesn't store data).

    Witness nodes are cheap to run and increase availability:
    - 2 full members + 1 witness = tolerates 1 failure
    - Witness only participates in leader election
    - No data replication overhead
    """
    witness_id: str       # "witness-cn-01"
    endpoint: str         # "http://cn-witness.nexus.io:2027"
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
        mode=ConsistencyMode.STRONG_HA,  # Use Raft for HA locks
        raft_group=RaftGroupConfig(
            group_id="locks-raft",
            members=[...],
            witnesses=[...],
        ),
    ),
    ConsensusZoneConfig(
        path_pattern="/global/*",
        mode=ConsistencyMode.STRONG_HA,
        raft_group=RaftGroupConfig(
            group_id="global-raft",
            members=[...],
            witnesses=[...],
        ),
    ),
]
```

### Phase 4: Raft-based Strong Consensus (STRONG_HA)

For paths requiring both strong consistency AND high availability, we use Raft consensus with optional witness nodes.

#### 4.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│  STRONG_HA: Raft Consensus with Witness                                 │
│                                                                         │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐        │
│  │   Full Member   │  │   Full Member   │  │    Witness      │        │
│  │   nexus-cn-01   │  │   nexus-cn-02   │  │  witness-cn-01  │        │
│  │   (Leader)      │  │   (Follower)    │  │  (Vote only)    │        │
│  │                 │  │                 │  │                 │        │
│  │  ┌───────────┐  │  │  ┌───────────┐  │  │  ┌───────────┐  │        │
│  │  │   Data    │  │  │  │   Data    │  │  │  │  No Data  │  │        │
│  │  │  (Copy 1) │  │  │  │  (Copy 2) │  │  │  │           │  │        │
│  │  └───────────┘  │  │  └───────────┘  │  │  └───────────┘  │        │
│  │                 │  │                 │  │                 │        │
│  │  Vote: ✓        │  │  Vote: ✓        │  │  Vote: ✓        │        │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘        │
│           │                    │                    │                  │
│           └────────────────────┼────────────────────┘                  │
│                         Raft Protocol                                  │
│                    (Leader Election + Log Replication)                 │
│                                                                         │
│  Tolerates 1 failure (any node) while maintaining:                     │
│  - Strong consistency (linearizable)                                   │
│  - Automatic failover (<1s)                                            │
│  - Only 2x storage (not 3x, witness has no data)                       │
└─────────────────────────────────────────────────────────────────────────┘
```

#### 4.2 Why Raft + Witness?

| Approach | Nodes | Storage | Tolerates | Consistency |
|----------|-------|---------|-----------|-------------|
| STRONG (single) | 1 | 1x | 0 failures | ✓ Linearizable |
| QUORUM (3 nodes) | 3 | 3x | 1 failure | ✓ Linearizable |
| **STRONG_HA (2+1)** | **2 full + 1 witness** | **2x** | **1 failure** | **✓ Linearizable** |

**Witness benefits:**
- Cheap: No data storage, minimal CPU/memory
- Fast election: Participates in leader election
- Cost-effective HA: 2 data copies instead of 3

#### 4.3 Integration with tikv/raft-rs

We leverage the battle-tested `tikv/raft-rs` Rust library (same as TiKV, CockroachDB).

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Nexus Raft Integration                                                 │
│                                                                         │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  Python Layer (nexus/)                                           │  │
│  │                                                                   │  │
│  │  ZoneAwareNexusFS                                                │  │
│  │       │                                                          │  │
│  │       ▼                                                          │  │
│  │  RaftConsensusManager (Python)                                   │  │
│  │       │                                                          │  │
│  │       │ PyO3 FFI                                                 │  │
│  │       ▼                                                          │  │
│  └───────┼──────────────────────────────────────────────────────────┘  │
│          │                                                              │
│  ┌───────┼──────────────────────────────────────────────────────────┐  │
│  │  Rust Layer (rust/nexus_fast/)                                   │  │
│  │       │                                                          │  │
│  │       ▼                                                          │  │
│  │  nexus_raft module                                               │  │
│  │       │                                                          │  │
│  │       ├── RaftNode (wraps raft-rs)                               │  │
│  │       ├── RaftStorage (log + snapshot)                           │  │
│  │       └── RaftTransport (gRPC between nodes)                     │  │
│  │                                                                   │  │
│  │  Dependencies:                                                    │  │
│  │  - tikv/raft-rs: Core Raft algorithm                             │  │
│  │  - tonic: gRPC for Raft messages                                 │  │
│  │  - sled/rocksdb: Raft log storage                                │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

#### 4.4 Rust Module: nexus_raft

```rust
// rust/nexus_fast/src/raft/mod.rs

use raft::{Config, RawNode, Storage};
use pyo3::prelude::*;

/// Nexus Raft node wrapping tikv/raft-rs
#[pyclass]
pub struct NexusRaftNode {
    node: RawNode<MemStorage>,
    peers: Vec<u64>,
    is_witness: bool,
}

#[pymethods]
impl NexusRaftNode {
    #[new]
    fn new(node_id: u64, peers: Vec<u64>, is_witness: bool) -> PyResult<Self> {
        let config = Config {
            id: node_id,
            election_tick: 10,
            heartbeat_tick: 1,
            ..Default::default()
        };

        let storage = if is_witness {
            // Witness: minimal storage, no data log
            MemStorage::new_witness()
        } else {
            // Full member: full log + data
            MemStorage::new()
        };

        let node = RawNode::new(&config, storage)?;
        Ok(Self { node, peers, is_witness })
    }

    /// Propose a write operation (only on leader)
    fn propose(&mut self, data: Vec<u8>) -> PyResult<()> {
        self.node.propose(vec![], data)?;
        Ok(())
    }

    /// Check if this node is the leader
    fn is_leader(&self) -> bool {
        self.node.raft.state == StateRole::Leader
    }

    /// Get current leader ID
    fn leader_id(&self) -> Option<u64> {
        let leader = self.node.raft.leader_id;
        if leader == 0 { None } else { Some(leader) }
    }

    /// Process a tick (call periodically)
    fn tick(&mut self) {
        self.node.tick();
    }

    /// Process ready state (messages to send, entries to apply)
    fn process_ready(&mut self) -> PyResult<RaftReady> {
        if !self.node.has_ready() {
            return Ok(RaftReady::empty());
        }
        let ready = self.node.ready();
        // ... process messages, entries, snapshots
        Ok(RaftReady::from(ready))
    }
}
```

#### 4.5 Python Integration

```python
# src/nexus/consensus/raft_manager.py

from nexus_fast import NexusRaftNode  # PyO3 import

class RaftConsensusManager:
    """Manages Raft consensus groups for STRONG_HA zones."""

    def __init__(self, local_box_id: str):
        self.local_box_id = local_box_id
        self._groups: dict[str, RaftGroup] = {}

    async def create_group(
        self,
        config: RaftGroupConfig,
    ) -> RaftGroup:
        """Create or join a Raft group."""
        node_id = self._box_id_to_node_id(self.local_box_id)
        peer_ids = [self._box_id_to_node_id(m.box_id) for m in config.members]
        is_witness = self._is_witness(self.local_box_id, config)

        # Create Rust Raft node
        raft_node = NexusRaftNode(
            node_id=node_id,
            peers=peer_ids,
            is_witness=is_witness,
        )

        group = RaftGroup(
            config=config,
            node=raft_node,
            transport=RaftTransport(config.members + config.witnesses),
        )

        self._groups[config.group_id] = group
        return group

    async def propose_write(
        self,
        group_id: str,
        path: str,
        content: bytes,
    ) -> WriteResult:
        """Propose a write through Raft consensus."""
        group = self._groups[group_id]

        if not group.node.is_leader():
            # Forward to leader
            leader_id = group.node.leader_id()
            if leader_id is None:
                raise NoLeaderError("No leader elected")
            return await group.transport.forward_write(leader_id, path, content)

        # We are leader - propose to Raft
        proposal = WriteProposal(path=path, content=content)
        group.node.propose(proposal.serialize())

        # Wait for commit
        return await group.wait_for_commit(proposal.id)


class RaftGroup:
    """A single Raft consensus group."""

    def __init__(
        self,
        config: RaftGroupConfig,
        node: NexusRaftNode,
        transport: RaftTransport,
    ):
        self.config = config
        self.node = node
        self.transport = transport
        self._pending: dict[str, asyncio.Future] = {}
        self._tick_task: asyncio.Task | None = None

    async def start(self):
        """Start the Raft tick loop."""
        self._tick_task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self):
        """Periodic tick for leader election and heartbeats."""
        while True:
            self.node.tick()
            ready = self.node.process_ready()

            # Send Raft messages to peers
            for msg in ready.messages:
                await self.transport.send(msg)

            # Apply committed entries
            for entry in ready.committed_entries:
                await self._apply_entry(entry)

            await asyncio.sleep(0.01)  # 10ms tick
```

#### 4.6 Witness Node Implementation

```python
# src/nexus/consensus/witness.py

class RaftWitnessServer:
    """Lightweight Raft witness server.

    A witness participates in leader election but doesn't store data.
    This allows 2-node HA with strong consistency at lower cost.
    """

    def __init__(self, witness_id: str, port: int):
        self.witness_id = witness_id
        self.port = port
        self._groups: dict[str, NexusRaftNode] = {}

    async def join_group(self, config: RaftGroupConfig) -> None:
        """Join a Raft group as witness."""
        node_id = self._witness_id_to_node_id(self.witness_id)
        peer_ids = [self._box_id_to_node_id(m.box_id) for m in config.members]

        # Create witness node (no data storage)
        self._groups[config.group_id] = NexusRaftNode(
            node_id=node_id,
            peers=peer_ids,
            is_witness=True,  # Key difference!
        )

    async def handle_vote_request(self, group_id: str, request: VoteRequest) -> VoteResponse:
        """Handle Raft vote request."""
        group = self._groups[group_id]
        # Witness votes but doesn't need to check log
        return group.node.handle_vote(request)

    async def handle_heartbeat(self, group_id: str, request: Heartbeat) -> HeartbeatResponse:
        """Handle leader heartbeat."""
        group = self._groups[group_id]
        return group.node.handle_heartbeat(request)

    # Note: Witnesses don't handle AppendEntries (no data)
```

#### 4.7 Deployment: Witness as Sidecar or Standalone

```yaml
# docker-compose.demo.yml - Witness as lightweight sidecar

services:
  nexus-cn-01:
    image: nexus-server:latest
    environment:
      NEXUS_RAFT_ENABLED: "true"
      NEXUS_RAFT_NODE_ID: "1"
      NEXUS_RAFT_PEERS: "nexus-cn-02:2026,witness-cn:2027"

  nexus-cn-02:
    image: nexus-server:latest
    environment:
      NEXUS_RAFT_ENABLED: "true"
      NEXUS_RAFT_NODE_ID: "2"
      NEXUS_RAFT_PEERS: "nexus-cn-01:2026,witness-cn:2027"

  witness-cn:
    image: nexus-witness:latest  # Minimal image, ~10MB
    environment:
      NEXUS_WITNESS_ID: "witness-cn"
      NEXUS_RAFT_PEERS: "nexus-cn-01:2026,nexus-cn-02:2026"
    deploy:
      resources:
        limits:
          memory: 64M  # Witness is very lightweight
          cpus: '0.1'
```

## Example: Multi-DC Configuration

### Deployment Topology

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Tenant: "acme_corp" - Multi-DC with STRONG_HA (Raft + Witness)             │
│                                                                              │
│  China Region (HA Cluster)        US Region            EU Region            │
│  ┌─────────────┬─────────────┐   ┌─────────────┐      ┌─────────────┐      │
│  │ nexus-cn-01 │ nexus-cn-02 │   │ nexus-us-01 │      │ nexus-eu-01 │      │
│  │  (Leader)   │ (Follower)  │   │   /us/*     │      │   /eu/*     │      │
│  │   /cn/*     │   /cn/*     │   │             │      │             │      │
│  └──────┬──────┴──────┬──────┘   └─────────────┘      └─────────────┘      │
│         │    Raft     │                                                      │
│         └──────┬──────┘                                                      │
│         ┌──────┴──────┐                                                      │
│         │ witness-cn  │  ← Lightweight witness (vote only, no data)         │
│         │  (64MB RAM) │                                                      │
│         └─────────────┘                                                      │
│                                                                              │
│  Benefits:                                                                   │
│  - nexus-cn-01 fails → nexus-cn-02 becomes leader in <1s                    │
│  - Only 2x storage (not 3x), witness has no data                            │
│  - Strong consistency maintained during failover                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Zone Configuration

```python
# Example configuration for acme_corp tenant
zones = [
    # China region - STRONG_HA with Raft (tolerates 1 node failure)
    ConsensusZoneConfig(
        path_pattern="/cn/*",
        mode=ConsistencyMode.STRONG_HA,
        raft_group=RaftGroupConfig(
            group_id="cn-raft",
            members=[
                RaftMember(box_id="nexus-cn-01", endpoint="http://cn1.nexus.io:2026"),
                RaftMember(box_id="nexus-cn-02", endpoint="http://cn2.nexus.io:2026"),
            ],
            witnesses=[
                RaftWitness(witness_id="witness-cn", endpoint="http://cn-w.nexus.io:2027"),
            ],
        ),
    ),

    # US/EU regions - simple STRONG (single authority, no HA needed)
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

    # Critical global config - STRONG_HA across regions
    ConsensusZoneConfig(
        path_pattern="/global/config/*",
        mode=ConsistencyMode.STRONG_HA,
        raft_group=RaftGroupConfig(
            group_id="global-raft",
            members=[
                RaftMember(box_id="nexus-cn-01", endpoint="http://cn1.nexus.io:2026"),
                RaftMember(box_id="nexus-us-01", endpoint="http://us1.nexus.io:2026"),
            ],
            witnesses=[
                RaftWitness(witness_id="witness-eu", endpoint="http://eu-w.nexus.io:2027"),
            ],
        ),
    ),

    # Locks - STRONG_HA for reliability
    ConsensusZoneConfig(
        path_pattern="/*/locks/*",
        mode=ConsistencyMode.STRONG_HA,
        raft_group=RaftGroupConfig(
            group_id="locks-raft",
            members=[
                RaftMember(box_id="nexus-cn-01", endpoint="http://cn1.nexus.io:2026"),
                RaftMember(box_id="nexus-us-01", endpoint="http://us1.nexus.io:2026"),
            ],
            witnesses=[
                RaftWitness(witness_id="witness-eu", endpoint="http://eu-w.nexus.io:2027"),
            ],
        ),
    ),
]
```

### Operation Examples

```python
# Agent in China writes to local path - FAST + HA
await nx.write("/cn/agent-output/result.json", data)
# → Write to Raft leader (nexus-cn-01 or nexus-cn-02)
# → Replicated to follower before ACK
# → Latency: <20ms (local Raft)
# → If leader fails, automatic failover in <1s

# Agent in China reads US data - SLOWER (cross-region)
content = await nx.read("/us/shared/model.bin")
# → Forward to nexus-us-01 (cross-Pacific)
# → Latency: 150-200ms

# Agent in China reads cached data - FAST (may be stale)
content = await nx.read("/shared/cache/config.json")
# → Read from local replica (nexus-cn-01)
# → Latency: <10ms
# → May be up to 10s stale

# Lock for meeting floor control - STRONG_HA
lock_id = await nx.lock("/cn/meeting/floor")
# → Acquired via Raft consensus
# → Leader election ensures no split-brain
# → If leader fails during lock, new leader continues
```

### Docker Integration Testing

```yaml
# docker-compose.test-federation.yml
# Test multi-box federation with Raft locally

services:
  nexus-box-1:
    image: nexus-server:latest
    environment:
      NEXUS_BOX_ID: "box-1"
      NEXUS_RAFT_ENABLED: "true"
      NEXUS_RAFT_PEERS: "box-2:2026,witness:2027"
    ports:
      - "2026:2026"

  nexus-box-2:
    image: nexus-server:latest
    environment:
      NEXUS_BOX_ID: "box-2"
      NEXUS_RAFT_ENABLED: "true"
      NEXUS_RAFT_PEERS: "box-1:2026,witness:2027"
    ports:
      - "2027:2026"

  witness:
    image: nexus-witness:latest
    environment:
      NEXUS_WITNESS_ID: "witness-1"
      NEXUS_RAFT_PEERS: "box-1:2026,box-2:2026"
    ports:
      - "2028:2027"
    deploy:
      resources:
        limits:
          memory: 64M

  # Test runner
  test:
    image: python:3.13
    command: pytest tests/integration/test_federation.py -v
    depends_on:
      - nexus-box-1
      - nexus-box-2
      - witness
```

## Implementation Roadmap

### Block 2: P2P Foundation (Current Focus)

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

### Block 3: Consensus Zones (STRONG, EVENTUAL, QUORUM)

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

### Block 4: STRONG_HA with Raft + Witness

| Component | Language | Effort | Priority |
|-----------|----------|--------|----------|
| nexus_raft module | Rust | ~500 lines | P0 |
| RaftStorage (sled backend) | Rust | ~200 lines | P0 |
| RaftTransport (gRPC) | Rust | ~300 lines | P0 |
| PyO3 bindings | Rust | ~150 lines | P0 |
| RaftConsensusManager | Python | ~200 lines | P0 |
| RaftWitnessServer | Python | ~100 lines | P0 |
| Integration tests | Python | ~200 lines | P0 |
| **Total** | **Rust+Python** | **~1650 lines** | |

**Dependencies:**
- `tikv/raft-rs` - Core Raft algorithm (battle-tested)
- `tonic` - gRPC for Raft message transport
- `sled` or `rocksdb` - Raft log persistence

**Deliverables:**
- STRONG_HA mode with automatic leader election
- Witness nodes for cost-effective HA (2 full + 1 witness)
- Sub-second failover on leader failure
- Docker-based integration testing

### Block 5: Production Hardening

| Component | Effort | Priority |
|-----------|--------|----------|
| Failover handling | ~200 lines | P0 |
| Health monitoring | ~150 lines | P0 |
| Metrics & observability | ~100 lines | P1 |
| Admin CLI for zones | ~100 lines | P2 |
| Witness deployment tooling | ~100 lines | P2 |

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
