//! Nexus Witness Node
//!
//! A lightweight Raft witness that participates in leader election
//! but doesn't store data. This enables cost-effective high availability
//! with only 2 full nodes + 1 witness.
//!
//! # What is a Witness?
//!
//! - Votes in leader elections ✓
//! - Stores Raft log (for vote validation) ✓
//! - Does NOT store state machine ✗
//! - Does NOT store file data ✗
//! - Cannot become leader ✗
//!
//! # Usage
//!
//! ```bash
//! # Start a witness node
//! nexus-witness --id witness-1 --peers node1:2026,node2:2026 --port 2027
//! ```
//!
//! # Resource Requirements
//!
//! - Memory: ~64MB (just Raft log, no data)
//! - CPU: <0.1 core (only processes votes/heartbeats)
//! - Disk: ~1GB (Raft log only, auto-compacted)

fn main() {
    eprintln!("Nexus Witness Node");
    eprintln!("==================");
    eprintln!();
    eprintln!("This binary is a placeholder. Full implementation coming in Commit 3.");
    eprintln!();
    eprintln!("The witness node will:");
    eprintln!("  - Use sled for Raft log storage (Commit 1 ✓)");
    eprintln!("  - Use gRPC for Raft transport (Commit 2)");
    eprintln!("  - Implement tikv/raft-rs (Commit 3)");
    eprintln!();
    eprintln!("See: docs/architecture/p2p-federation-consensus-zones.md");

    std::process::exit(1);
}
