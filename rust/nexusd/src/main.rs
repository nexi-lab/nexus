//! `nexusd-full` — the nexus full daemon assembly binary.
//!
//! The composition root: it hands the kernel the ordered `ServiceDecl`
//! set — a2a (from-stamp) + managed_agent (raw ACP control plane) + acp
//! (one-shot) — which `Kernel::bring_up_services` installs after the
//! kernel + auth are up. All the clap parsing, tokio runtime, and
//! kernel/raft/federation boot flow through `nexus_cluster::run_with_services`
//! unchanged; the only delta from the pure nexus-vfs `nexusd-full` is the
//! service set, which lives HERE (next to the service crates it links)
//! because the kernel repo can't depend on the nexus service crates.
//!
//! Supersedes the nexus-vfs `nexusd-full` (kernel + drivers, no nexus
//! services) as the production daemon — the Dockerfiles build this binary.
//! `driver-s3` matches the `full` profile via `backends` feature unification.

fn main() -> anyhow::Result<()> {
    // The daemon's service set — the SSOT for "which services this daemon
    // runs", ordered. a2a is installed first (its from-stamp hook must be
    // armed before agents write mailboxes); managed_agent + acp follow.
    // `ctx.auth_armed` is boot-derived (true iff sk- auth is armed) and
    // sets a2a's fail-closed posture; acp's default zone is the node-local
    // root.
    nexus_cluster::run_with_services(|ctx| {
        vec![
            a2a::service_decl(ctx.auth_armed),
            services::managed_agent::service_decl(),
            services::acp::service_decl("root".to_string()),
        ]
    })
}
