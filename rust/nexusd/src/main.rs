//! `nexusd-cluster` — the nexus cluster-profile daemon assembly binary.
//!
//! This IS the `cluster` deployment profile (KERNEL-ARCHITECTURE §7:
//! slim ⊂ cluster ⊂ … ⊂ full) plus the agent control plane. The
//! composition root hands the kernel the ordered `ServiceDecl` set —
//! a2a (from-stamp) + managed_agent (raw ACP control plane) — which
//! `Kernel::bring_up_services` installs after the kernel + auth are up.
//! All clap parsing, the tokio runtime, and kernel/raft/federation boot
//! flow through `nexus_cluster::run_with_services` unchanged; the only
//! delta from the pure nexus-vfs `nexusd-cluster` is the service set,
//! which lives HERE (next to the service crates it links) because the
//! kernel repo can't depend on the nexus service crates.
//!
//! Profile purity (§7): the DEFAULT build stays the slim cluster —
//! backends = path-local + remote only (via `nexus-cluster`), no S3, no
//! connectors — so the shipped binary keeps the sudowork/edge size
//! budget. The full/S3 superset is the opt-in `driver-s3` feature, never
//! the default. managed_agent is the one control-plane service cluster
//! adds; supersets inherit it. `acp` (nexus-drives-ACP one-shot) is NOT a
//! cluster service.

#[cfg(feature = "cohost-sudocode")]
mod cohost;

/// The `managed_agent` boot declaration. In the default slim cluster build
/// this is the procfs-only variant (`service_decl`) — no runtime body. In a
/// `cohost-sudocode` build it becomes a custom decl whose `install` injects
/// the real sudocode runtime via `install_managed_agent_with_spawn`, so a
/// minted agent runs a live LLM loop in-process. Same service name either
/// way; only the install closure differs.
#[cfg(not(feature = "cohost-sudocode"))]
fn managed_agent_decl() -> kernel::kernel::ServiceDecl {
    managed_agent::service_decl()
}

#[cfg(feature = "cohost-sudocode")]
fn managed_agent_decl() -> kernel::kernel::ServiceDecl {
    use std::sync::Arc;
    kernel::kernel::ServiceDecl {
        name: "managed_agent".to_string(),
        install: Box::new(|kernel| {
            managed_agent::install_managed_agent_with_spawn(
                kernel,
                Arc::new(cohost::SudoCodeSpawnAdapter),
            )
        }),
    }
}

fn main() -> anyhow::Result<()> {
    // The daemon's service set — the SSOT for "which services this daemon
    // runs", ordered. a2a is installed first (its from-stamp hook must be
    // armed before agents write mailboxes); managed_agent follows.
    // `ctx.auth_armed` is boot-derived (true iff sk- auth is armed) and
    // sets a2a's fail-closed posture.
    nexus_cluster::run_with_services(|ctx| {
        vec![a2a::service_decl(ctx.auth_armed), managed_agent_decl()]
    })
}
