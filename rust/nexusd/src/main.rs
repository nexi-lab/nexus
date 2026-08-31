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
//!
//! Opt-in features (each linked ONLY when named on the cargo cmdline):
//!
//!   * `driver-s3` — adds the S3 / R2 object-store driver.
//!   * `http-api` — adds the axum HTTP shim over the search-plugin gRPC
//!     surface (part of R10 epic #4674). Runtime bring-up ALSO needs
//!     `NEXUS_HTTP_ADDR` set.
//!   * `cohost-sudocode` — links the sudocode agent runtime IN-PROCESS
//!     (cross-repo pin coordination required).
//!   * `rebac` — links `nexus-rebac` (the ReBAC enforcer) and installs
//!     its `PermissionProvider` in the kernel's ONE slot. Backed by
//!     `RaftReBACTupleStore` over the SAME credential-zone consensus
//!     that already backs `RaftAuthKeyStore` (one namespace over —
//!     `CONTROL_NS_REBAC = "rebac"`).  Rust replacement for the Python
//!     `bricks/rebac/` (R10 epic #4674).
//!   * `full` — meta-feature = `driver-s3 + http-api + rebac`. Does NOT
//!     include `cohost-sudocode` (that one needs cross-repo pin
//!     coordination; operators wanting the true everything-build spell
//!     `--features full,cohost-sudocode`).

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
    // Parse HTTP-API config UPFRONT — outside `run_with_services` —
    // so a malformed `NEXUS_HTTP_ADDR` fails `main()` before the
    // daemon does any boot work.  `run_with_services`'s closure
    // returns `Vec<ServiceDecl>` (infallible), so any Err on the
    // env path has to raise before the closure runs.  Feature-off
    // build stubs to `Ok(None)` — no CLI parse cost.
    let http_api_config = parse_http_api_config()?;

    // The daemon's service set — the SSOT for "which services this daemon
    // runs", ordered. a2a is installed first (its from-stamp hook must be
    // armed before agents write mailboxes); managed_agent follows; the
    // optional HTTP-API listener (feature-gated `http-api`) slots in last;
    // the optional ReBAC enforcer (feature-gated `rebac`) installs its
    // `PermissionProvider` at the same tier as the HTTP-API decl.
    // `ctx.auth_armed` is boot-derived (true iff sk- auth is armed) and
    // sets a2a's fail-closed posture; `ctx.auth` + `ctx.runtime` are live
    // handles that http-api needs; `ctx.credential_consensus` +
    // `ctx.credential_zone_runtime` are what the rebac decl binds its
    // `RaftReBACTupleStore` over (same consensus as `RaftAuthKeyStore`,
    // one namespace over).
    nexus_cluster::run_with_services(move |ctx| {
        let mut decls = vec![a2a::service_decl(ctx.auth_armed), managed_agent_decl()];
        if let Some(decl) = http_api_decl_from(&http_api_config, ctx) {
            decls.push(decl);
        }
        if let Some(decl) = rebac_decl_from(ctx) {
            decls.push(decl);
        }
        decls
    })
}

/// HTTP-API config parsed at boot from env — cheap, refused loud
/// on typos.  `Some(_)` when the operator opted in AND the address
/// parses; `None` when the operator did not opt in (feature off,
/// or `NEXUS_HTTP_ADDR` unset).
///
/// Kept as a plain struct (not `Option<SocketAddr>` naked) so
/// adding a future field (`NEXUS_HTTP_TLS_CERT`, etc.) does not
/// ripple through `main` — additive.
#[cfg(feature = "http-api")]
struct HttpApiConfig {
    addr: std::net::SocketAddr,
    upstream_grpc: String,
}

/// Feature-off stub — kept `Option`-shaped so the call site stays
/// symmetric (`if let Some(...)`).  Using `()` as the payload so
/// no dead `struct HttpApiConfig` shows up in the slim build.
#[cfg(not(feature = "http-api"))]
type HttpApiConfig = ();

/// Parse `NEXUS_HTTP_ADDR` + optional `NEXUS_HTTP_UPSTREAM_GRPC`.
///
/// # Return
///
/// * `Ok(None)` — the `http-api` feature is off, OR the operator
///   did not set `NEXUS_HTTP_ADDR` (opt-out by omission).  Silent
///   is CORRECT here: no intent expressed, nothing to install.
/// * `Ok(Some(cfg))` — feature on, env set, address parsed — the
///   real bring-up path will install the decl.
/// * `Err(_)` — feature on, `NEXUS_HTTP_ADDR` set but MALFORMED.
///   Fails the whole daemon boot rather than silent-warn-and-skip
///   (`feedback_fail_loud_interdependent_config` — the operator
///   opted in; a typo must fail visibly, not degrade to a running
///   daemon with no HTTP surface + a lone log line the operator
///   might miss).
///
/// Upstream gRPC target defaults to `http://127.0.0.1:2126` (the
/// co-hosted gRPC surface — loopback saves the round-trip);
/// `NEXUS_HTTP_UPSTREAM_GRPC` overrides for the split-binary case.
#[cfg(feature = "http-api")]
fn parse_http_api_config() -> anyhow::Result<Option<HttpApiConfig>> {
    let Some(addr_str) = std::env::var("NEXUS_HTTP_ADDR").ok() else {
        return Ok(None);
    };
    let addr: std::net::SocketAddr = addr_str.parse().map_err(|e| {
        anyhow::anyhow!(
            "NEXUS_HTTP_ADDR={addr_str:?} is not a valid SocketAddr: {e} — \
             refusing to boot the daemon (operator opted in; a typo must \
             surface loudly, not degrade to a running daemon with no HTTP \
             surface).  Expected shape: `HOST:PORT` (e.g. `0.0.0.0:2128`).",
        )
    })?;
    let upstream_grpc = std::env::var("NEXUS_HTTP_UPSTREAM_GRPC")
        .unwrap_or_else(|_| "http://127.0.0.1:2126".to_string());
    Ok(Some(HttpApiConfig {
        addr,
        upstream_grpc,
    }))
}

#[cfg(not(feature = "http-api"))]
fn parse_http_api_config() -> anyhow::Result<Option<HttpApiConfig>> {
    Ok(None)
}

/// Build the HTTP-API `ServiceDecl` from a parsed config + boot ctx.
/// Split from `parse_http_api_config` because the ctx (with the live
/// auth provider + runtime handle) is only available inside
/// `run_with_services`'s closure, but the config parse must run
/// upfront to fail-loud on env typos.
#[cfg(feature = "http-api")]
fn http_api_decl_from(
    config: &Option<HttpApiConfig>,
    ctx: &nexus_cluster::ServiceBootCtx,
) -> Option<kernel::kernel::ServiceDecl> {
    use std::sync::Arc;
    let cfg = config.as_ref()?;
    Some(nexus_http_api::service_decl(
        cfg.addr,
        cfg.upstream_grpc.clone(),
        Arc::clone(&ctx.auth),
        ctx.runtime.clone(),
    ))
}

#[cfg(not(feature = "http-api"))]
fn http_api_decl_from(
    _config: &Option<HttpApiConfig>,
    _ctx: &nexus_cluster::ServiceBootCtx,
) -> Option<kernel::kernel::ServiceDecl> {
    None
}

/// Build the ReBAC enforcer `ServiceDecl` from the boot ctx.
///
/// # What it does
///
/// * Constructs a `RaftReBACTupleStore` over `ctx.credential_
///   consensus` — the SAME consensus that already backs
///   `RaftAuthKeyStore`, one namespace over (`CONTROL_NS_REBAC =
///   "rebac"`).  Grants + revokes therefore replicate through the
///   same raft log a key mint does — one cluster-wide plane, no
///   split-brain between auth and access.
/// * Wraps in `ReBACGraphCache` for the per-zone `Arc<ReBACGraph>`
///   cache the enforcer's check hot path reads.
/// * Wraps in `RebacPermissionProvider` and hands it to
///   `kernel.set_permission_provider(...)` in the install closure.
///
/// # Why a `ServiceDecl` (not a direct call at ctx-build time)
///
/// The kernel's `Arc<Kernel>` handle is only available inside a
/// `ServiceDecl::install` closure — the ServiceRegistry is the single
/// authority for services (`feedback_sr_uniform_service_registration`).
/// Even for a wiring-only "service" that installs no RPC surface, going
/// through the decl keeps the composition-root uniform + boot-ordered.
///
/// # Feature-off build stubs to `None`
///
/// The `rebac`-off build never links `nexus-rebac`, so the entire
/// enforcer + graph engine + `lib::rebac` transitive weight stays
/// out of the slim `cluster` binary (§7 size budget).
#[cfg(feature = "rebac")]
fn rebac_decl_from(ctx: &nexus_cluster::ServiceBootCtx) -> Option<kernel::kernel::ServiceDecl> {
    use std::sync::Arc;
    // Build the store + cache + provider OUTSIDE the install
    // closure so ctx borrows do not need to move into a `'static`
    // closure (the ServiceInstall signature is `FnOnce(&Arc<
    // Kernel>) + Send + 'static`).  `store` clones the consensus +
    // runtime handle; both are Arc-inside — cheap.
    let store: Arc<dyn nexus_rebac::ReBACTupleStore> = nexus_rebac::RaftReBACTupleStore::new_arc(
        ctx.credential_consensus.clone(),
        ctx.credential_zone_runtime.clone(),
    );
    let cache = Arc::new(nexus_rebac::ReBACGraphCache::new(store));
    let provider: Arc<Box<dyn kernel::core::dispatch::PermissionProvider>> =
        Arc::new(Box::new(nexus_rebac::RebacPermissionProvider::new(cache)));
    Some(kernel::kernel::ServiceDecl {
        name: "rebac".to_string(),
        install: Box::new(move |kernel| {
            kernel.set_permission_provider(provider);
            Ok(())
        }),
    })
}

#[cfg(not(feature = "rebac"))]
fn rebac_decl_from(_ctx: &nexus_cluster::ServiceBootCtx) -> Option<kernel::kernel::ServiceDecl> {
    None
}
