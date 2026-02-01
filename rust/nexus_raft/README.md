# nexus_raft

Raft consensus and embedded storage for Nexus STRONG_HA zones.

## Overview

`nexus_raft` provides infrastructure for distributed consensus in Nexus:

1. **Embedded Storage** (Commit 1 - This PR) - General-purpose sled-based KV store
2. **gRPC Transport** (Commit 2) - Raft message transport using tonic
3. **Raft Consensus** (Commit 3) - tikv/raft-rs integration
4. **Witness Node** (Commit 3) - Lightweight vote-only node

## Modules

### Transport (`transport/`) - Commit 2

A gRPC transport layer based on [tonic](https://github.com/hyperium/tonic).

**Why gRPC?**
- Streaming: Native support for bidirectional streams (ideal for Raft heartbeats)
- Efficiency: HTTP/2 multiplexing, long-lived connections
- Code generation: Less boilerplate than manual HTTP
- Compatibility: Works with tikv/raft-rs patterns

**Reusable for:**
- Raft messages (primary use case)
- Webhook streaming (Server Streaming replaces HTTP POST)
- Real-time event push

```rust
use nexus_raft::transport::{RaftClient, RaftClientPool, NodeAddress};

// Create a client pool
let pool = RaftClientPool::new();

// Get a client for a node
let addr = NodeAddress::new(1, "http://10.0.0.1:2026");
let client = pool.get(&addr).await?;

// Send Raft messages
let response = client.request_vote(term, candidate_id, last_log_index, last_log_term).await?;
```

**Streaming Patterns:**
| Pattern | Use Case | Example |
|---------|----------|---------|
| Unary | Request/response | Vote requests |
| Server Streaming | Push to client | Webhook events |
| Client Streaming | Bulk upload | Snapshots |
| Bidirectional | Real-time sync | Heartbeats |

**Feature flag required:**
```toml
[dependencies]
nexus_raft = { version = "0.1", features = ["grpc"] }
```

### Storage (`storage/`)

A general-purpose embedded key-value database based on [sled](https://github.com/spacejam/sled).

**Why sled?**
- Pure Rust: No C++ dependencies, easy cross-platform builds
- Embedded: No network latency, works during network partitions
- ACID: Crash-safe with write-ahead logging
- Fast: Lock-free concurrent reads, batch writes

**Reusable for:**
- Raft log storage (primary use case)
- Local persistent cache
- Task/event queues
- Session storage

```rust
use nexus_raft::storage::SledStore;

// Open a database
let store = SledStore::open("/var/lib/nexus/data").unwrap();

// Use named trees (namespaces)
let raft_log = store.tree("raft_log").unwrap();
let cache = store.tree("cache").unwrap();

// Basic operations
raft_log.set(b"entry:1", b"data").unwrap();
cache.set_bincode(b"item:1", &my_struct).unwrap();
```

## Installation

### Prerequisites

- Rust toolchain (install via [rustup](https://rustup.rs/))

```bash
# Install Rust
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh

# Windows: Download from https://rustup.rs/
```

### Build

```bash
cd rust/nexus_raft

# Build library
cargo build --release

# Run tests
cargo test

# Build witness binary
cargo build --release --bin nexus-witness
```

### Build Output

- `target/release/libnexus_raft.rlib` - Rust library for other crates
- `target/release/nexus-witness` - Witness node binary (~10MB)

## Architecture

```
nexus_raft/
├── src/
│   ├── lib.rs           # Crate root, module exports
│   ├── storage/         # Embedded KV storage (sled)
│   │   ├── mod.rs       # Module exports
│   │   └── sled_store.rs # SledStore implementation
│   └── bin/
│       └── witness.rs   # Witness node binary
└── Cargo.toml
```

## API Reference

### SledStore

Main database handle.

```rust
// Open database
let store = SledStore::open(path)?;
let store = SledStore::open_temporary()?;  // For testing

// Get named tree
let tree = store.tree("my_tree")?;

// Basic KV operations (default tree)
store.set(key, value)?;
store.get(key)?;
store.delete(key)?;

// Utilities
store.flush()?;           // Force sync to disk
store.generate_id()?;     // Monotonic ID generation
```

### SledTree

Named tree (namespace) within a database.

```rust
let tree = store.tree("cache")?;

// Raw bytes
tree.set(key, value)?;
tree.get(key)?;
tree.delete(key)?;
tree.contains(key)?;

// Serialization
tree.set_json(key, &my_struct)?;     // JSON (human-readable)
tree.set_bincode(key, &my_struct)?;  // Bincode (fast, compact)
tree.get_json::<T>(key)?;
tree.get_bincode::<T>(key)?;

// Iteration
tree.iter();              // All entries
tree.range(start..end);   // Range scan
tree.scan_prefix(prefix); // Prefix scan

// Atomic operations
tree.compare_and_swap(key, expected, new)?;
tree.fetch_and_update(key, |old| new)?;

// Batch operations
let mut batch = SledBatch::new();
batch.insert(key1, value1);
batch.insert(key2, value2);
batch.remove(key3);
tree.apply_batch(&batch)?;
```

## Performance

sled provides excellent performance characteristics:

- **Reads**: Lock-free concurrent reads
- **Writes**: ~100,000 ops/sec (sequential), ~10,000 ops/sec (fsync'd)
- **Memory**: Configurable cache size
- **Disk**: Automatic compaction, ~2x write amplification

## References

- [sled](https://github.com/spacejam/sled) - Modern embedded database for Rust
- [tikv/raft-rs](https://github.com/tikv/raft-rs) - Raft implementation (Commit 3)
- [Design Doc](../../docs/architecture/p2p-federation-consensus-zones.md)
- Issue #1159: P2P Federation and Consensus Zones
