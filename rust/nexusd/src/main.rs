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

fn main() -> anyhow::Result<()> {
    // The daemon's service set — the SSOT for "which services this daemon
    // runs", ordered. a2a is installed first (its from-stamp hook must be
    // armed before agents write mailboxes); managed_agent follows.
    // `ctx.auth_armed` is boot-derived (true iff sk- auth is armed) and
    // sets a2a's fail-closed posture.
    nexus_cluster::run_with_services(|ctx| {
        vec![
            a2a::service_decl(ctx.auth_armed),
            services::managed_agent::service_decl(),
        ]
    })
}
