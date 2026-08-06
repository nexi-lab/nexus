//! `nexus-search-plugin` — SearchService cdylib.
//!
//! Loaded by `nexusd-cluster` at startup via `--plugin-dir`.  Exposes
//! `nexus.search.v1.SearchService` (recursive `Glob` + regex `Grep`
//! over the kernel VFS) through the Phase P plugin-as-gRPC-service
//! routing path — the plugin declares its service list via
//! `nexus_plugin_grpc_services`, the cluster's `PluginProxyService`
//! routes external tonic traffic at `/nexus.search.v1.SearchService/<M>`
//! into this plugin's `nexus_service_dispatch`.
//!
//! ## Why a plugin (and not a kernel primitive)
//!
//! Grep + glob are compositions over the kernel's `sys_readdir` +
//! `sys_read` primitives, not primitives themselves — putting the
//! walker + regex engine in the kernel tier would grow `kernel/`
//! with policy code that has no business next to the syscall ABI.
//! The cdylib boundary keeps the kernel small (kernel default:
//! zero search logic) and lets the plugin ship its own release
//! cadence with its own signed release chain.
//!
//! ## What v1 does
//!
//! Sequential recursive walk from `root_path` via `sys_readdir`.  Glob
//! matches via `globset`; grep reads file content via `sys_read` and
//! scans with the `regex` crate.  Results are unary responses capped
//! by `max_results` (default 10_000 for glob, 1_000 for grep).
//! Cross-node paths resolve transparently: `sys_readdir` returns
//! federation-mounted names, `sys_read` falls through to
//! `try_remote_fetch` on non-local paths — the plugin holds no
//! federation awareness of its own.
//!
//! v2 will add: streaming responses, trigram-indexed fast-path grep,
//! adaptive glob strategy per pattern shape, and (behind a feature
//! flag) semantic embeddings.  Kept separate from v1 to keep the
//! initial cdylib review surface small.

use std::ffi::c_char;
use std::sync::Arc;

use nexus_plugin_abi::{declare_service_plugin, KernelHandle};
use prost::Message;
use tonic::Request;

use crate::service::SearchServiceImpl;

// Generated tonic bindings for `nexus.search.v1`.  Include-path
// mirrors the vault crate — `OUT_DIR` gets the codegen output at
// build time from `build.rs`.
pub mod search_proto {
    #![allow(clippy::all)]
    #![allow(unused_qualifications)]
    tonic::include_proto!("nexus.search.v1");
}

pub mod ann_index;
pub mod chunker;
pub mod embedder;
pub mod fts_index;
pub mod fusion;
pub mod index_manager;
pub mod index_state;
pub mod kernel_io;
pub mod query_cache;
pub mod scoring;
pub mod service;

use search_proto::search_service_server::SearchService as SearchServiceTrait;
use search_proto::{GlobRequest, GrepRequest, IndexRequest, QueryRequest, RefreshRequest};

/// Plugin state held between `create` and `destroy`.
///
/// Owns the SearchService impl (which carries the compiled regex
/// cache and future indexing state) plus a small tokio runtime used
/// by `dispatch_grpc` to bridge the sync `nexus_service_dispatch`
/// ABI into the async tonic trait.
pub struct SearchPlugin {
    svc: Arc<SearchServiceImpl>,
    rt: tokio::runtime::Runtime,
}

/// Deep-clone a `KernelHandle` by copying every C-ABI field.  The
/// plugin ABI's `KernelHandle` is `#[repr(C)]` with fn-pointer +
/// `*const c_void` fields — all naturally `Copy` at the machine level
/// — but the type deliberately does NOT derive `Clone` so callers
/// think twice before duplicating it (each copy points at the same
/// kernel instance).  Search-plugin holds one copy inside an `Arc`
/// so the service impl + every `spawn_blocking` closure can share it
/// without cloning the underlying kernel.
fn dup_kernel_handle(h: &KernelHandle) -> KernelHandle {
    KernelHandle {
        sys_read: h.sys_read,
        sys_write: h.sys_write,
        sys_stat: h.sys_stat,
        sys_readdir: h.sys_readdir,
        sys_unlink: h.sys_unlink,
        sys_mkdir: h.sys_mkdir,
        sys_rmdir: h.sys_rmdir,
        sys_rename: h.sys_rename,
        sys_stat_batch: h.sys_stat_batch,
        kernel_ptr: h.kernel_ptr,
    }
}

fn create_search_plugin(kernel_handle: &KernelHandle) -> Box<SearchPlugin> {
    tracing::info!("nexus-search-plugin: create");
    let svc = Arc::new(SearchServiceImpl::new(Arc::new(dup_kernel_handle(
        kernel_handle,
    ))));
    // Tokio runtime build errors are effectively unreachable on
    // healthy hosts (would need OS-level thread creation failure);
    // panic here is same posture the sibling `nexus-vault` plugin
    // takes for its own service state — a broken plugin is worse
    // than a loud failure at load time.
    // Single-threaded runtime — same posture as `nexus-vault`.  Search
    // requests are IO-bound (kernel-syscall walk); parallelism gain
    // does not justify pulling `rt-multi-thread` into the plugin's
    // dep tree.
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .expect("build search-plugin tokio runtime");
    Box::new(SearchPlugin { svc, rt })
}

/// Legacy short-name Call RPC dispatch is intentionally empty — every
/// caller reaches us through Phase P gRPC routing.  The plugin ABI
/// still requires a dispatch function pointer, so we translate any
/// `/`-prefixed method into `dispatch_grpc` and reject the rest.
fn dispatch_search(plugin: &SearchPlugin, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
    if method.starts_with('/') {
        return dispatch_grpc(plugin, method, payload);
    }
    tracing::warn!(
        method = %method,
        "nexus-search-plugin: legacy Call RPC method rejected — use gRPC service path",
    );
    Err(-2 /* PluginResult::InvalidArgument */)
}

/// Bytes-level gRPC dispatch (Phase P contract).  `method` is the
/// full `/<svc.full.Name>/<Method>` path handed to us by the
/// cluster's `PluginProxyService`; `payload` is the prost-encoded
/// request body (gRPC frame already stripped upstream); return is
/// prost-encoded response body.
fn dispatch_grpc(plugin: &SearchPlugin, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
    // Strip leading `/` and split off service full name.
    let (service_and_method, _) = (method.trim_start_matches('/'), ());
    let mut parts = service_and_method.splitn(2, '/');
    let (Some(service_full_name), Some(method_name)) = (parts.next(), parts.next()) else {
        tracing::warn!(method = %method, "malformed gRPC path");
        return Err(-2);
    };
    if service_full_name != "nexus.search.v1.SearchService" {
        tracing::warn!(
            service = %service_full_name,
            method = %method_name,
            "unknown service",
        );
        return Err(-2);
    }

    match method_name {
        "Glob" => {
            let req = GlobRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Glob decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.glob(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Glob handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Grep" => {
            let req = GrepRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Grep decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.grep(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Grep handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Query" => {
            let req = QueryRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Query decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.query(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Query handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Index" => {
            let req = IndexRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Index decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.index(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Index handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Refresh" => {
            let req = RefreshRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Refresh decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.refresh(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Refresh handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        _ => {
            tracing::warn!(method = %method_name, "unknown method");
            Err(-2)
        }
    }
}

declare_service_plugin!("search", SearchPlugin, {
    create: create_search_plugin,
    dispatch: dispatch_search,
});

/// Phase P opt-in: this cdylib exposes the two gRPC service full names
/// hosted here, so `nexusd-cluster` can route external tonic traffic
/// at `/nexus.search.v1.SearchService/<method>` into our
/// `nexus_service_dispatch`.  See `nexus-plugin-abi`'s
/// `symbols::SERVICE_GRPC_SERVICES` constant for the JSON contract.
///
/// # Safety
///
/// `SERVICES_JSON` is a static, null-terminated UTF-8 byte string with
/// `'static` lifetime; the pointer we hand back never dangles.  The
/// loader treats the return value as `*const c_char` and only reads
/// (never frees) it.
#[no_mangle]
pub unsafe extern "C" fn nexus_plugin_grpc_services() -> *const c_char {
    const SERVICES_JSON: &[u8] = b"[\"nexus.search.v1.SearchService\"]\0";
    SERVICES_JSON.as_ptr() as *const c_char
}

// Re-export the two proto message types for downstream integration
// tests that dial the plugin over a real tonic channel.  Kept in the
// crate root so callers write `use nexus_search_plugin::{GlobRequest,
// GrepRequest}` without going through the generated module tree.
pub use search_proto::{GlobResponse as ExportGlobResponse, GrepResponse as ExportGrepResponse};
