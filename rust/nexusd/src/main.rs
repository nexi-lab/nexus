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

/// The `managed_agent` boot declaration. In the default slim cluster build
/// this is the procfs-only variant (`service_decl`) — no runtime body. In a
/// `cohost-sudocode` build it becomes a custom decl whose `install` injects
/// sudocode's `SudoCodeSpawnAdapter` via `install_managed_agent_with_spawn`,
/// so a minted agent runs a live LLM loop in-process. Same service name either
/// way; only the install closure differs. The adapter lives in sudocode
/// (`sudocode_tools::managed_agent`, next to the loop it wraps); this binary
/// just injects it — there is no nexus-side co-host glue.
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
                Arc::new(sudocode_tools::managed_agent::SudoCodeSpawnAdapter),
            )
        }),
    }
}

fn main() -> anyhow::Result<()> {
    // The daemon's service set — the SSOT for "which services this daemon
    // runs", ordered. a2a is installed first (its from-stamp hook must be
    // armed before agents write mailboxes); managed_agent follows; the
    // optional HTTP-API listener slots in last (its axum listener does
    // not depend on the other services' hooks, so ordering is free).
    // `ctx.auth_armed` is boot-derived (true iff sk- auth is armed) and
    // sets a2a's fail-closed posture; `ctx.auth` + `ctx.runtime` are
    // live handles that http-api needs to inject into its bearer
    // middleware + spawn its axum listener on the daemon runtime.
    nexus_cluster::run_with_services(|ctx| {
        let mut decls = vec![a2a::service_decl(ctx.auth_armed), managed_agent_decl()];
        if let Some(decl) = http_api_decl(ctx) {
            decls.push(decl);
        }
        decls
    })
}

/// Optional HTTP-API listener decl.  Present only when the operator
/// sets `NEXUS_HTTP_ADDR` (e.g. `NEXUS_HTTP_ADDR=0.0.0.0:2128`) —
/// keeps the default slim-cluster boot HTTP-surface-free (matches
/// the existing "gRPC on 2126, enroll on 2127, everything else
/// opt-in" posture).  A malformed value is logged + skipped
/// (fail-open on a config typo rather than refusing the whole
/// daemon boot); an operator diagnosing "why no HTTP" reads the
/// log line, an operator who never asked for HTTP is unaffected.
///
/// Upstream gRPC target defaults to `http://127.0.0.1:2126` because
/// the shim is co-hosted in this same binary (loopback saves the
/// TCP-out-and-back-in round-trip).  `NEXUS_HTTP_UPSTREAM_GRPC`
/// overrides for the split-binary case (e.g. serving the HTTP shim
/// from a separate pod pointing at a remote search-plugin cluster).
fn http_api_decl(ctx: &nexus_cluster::ServiceBootCtx) -> Option<kernel::kernel::ServiceDecl> {
    use std::sync::Arc;
    let addr_str = std::env::var("NEXUS_HTTP_ADDR").ok()?;
    let addr: std::net::SocketAddr = match addr_str.parse() {
        Ok(a) => a,
        Err(e) => {
            tracing::warn!(
                value = %addr_str,
                error = %e,
                "NEXUS_HTTP_ADDR set but not a valid SocketAddr — skipping HTTP-API bring-up",
            );
            return None;
        }
    };
    let upstream_grpc = std::env::var("NEXUS_HTTP_UPSTREAM_GRPC")
        .unwrap_or_else(|_| "http://127.0.0.1:2126".to_string());
    Some(nexus_http_api::service_decl(
        addr,
        upstream_grpc,
        Arc::clone(&ctx.auth),
        ctx.runtime.clone(),
    ))
}
