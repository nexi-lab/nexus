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
pub mod contextual_chunker;
pub mod embed_cache;
pub mod embedder;
pub mod fts_index;
pub mod fusion;
pub mod http_client;
pub mod index_manager;
pub mod index_seq;
pub mod index_state;
pub mod indexed_dirs_state;
pub mod internal_call;
pub mod kernel_io;
pub mod llm_chat;
pub mod macro_expand;
pub mod parked_state;
pub mod peer_fanout;
pub mod peer_registry;
pub mod query_cache;
pub mod query_expansion;
pub mod scoring;
pub mod service;
pub mod title_index;
pub mod zone_modes_state;

use search_proto::search_service_server::SearchService as SearchServiceTrait;
use search_proto::{
    AddIndexedDirectoryRequest, BatchQueryRequest, GlobRequest, GrepRequest, HealthRequest,
    IndexDocumentsRequest, IndexRequest, ListIndexedDirectoriesRequest,
    ListZoneIndexingModesRequest, LocateRequest, NotifyFileChangeRequest, ParkedDiscardRequest,
    ParkedListRequest, ParkedRetryRequest, QueryRequest, RefreshRequest,
    RemoveIndexedDirectoryRequest, SetZoneIndexingModeRequest, StatsRequest,
};

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
        free_buf: h.free_buf,
        kernel_ptr: h.kernel_ptr,
    }
}

fn create_search_plugin(kernel_handle: &KernelHandle) -> Box<SearchPlugin> {
    tracing::info!("nexus-search-plugin: create");
    let svc = Arc::new(SearchServiceImpl::new(Arc::new(dup_kernel_handle(
        kernel_handle,
    ))));
    Box::new(SearchPlugin {
        svc,
        rt: build_runtime(),
    })
}

/// Idle blocking-pool threads live this long before exiting (#4725).
/// tokio's default is 10 s, which under a steady request cadence means
/// a `pthread_create` per request — the churn that exhausted a cgroup
/// `pids.max` shared with the Python server.  It also means the pool is
/// usually EMPTY when a request arrives, and tokio turns a thread-spawn
/// `EAGAIN` into a panic only when the pool is empty (a non-empty pool
/// just queues the task for a busy thread).  Keeping threads for an
/// hour bounds the pool at the peak concurrency actually seen and makes
/// that panic unreachable while traffic flows.
const BLOCKING_THREAD_KEEP_ALIVE: std::time::Duration = std::time::Duration::from_secs(3600);

/// The plugin's tokio runtime — bridges the sync plugin-ABI dispatch
/// into the async tonic trait via `block_on`.
///
/// Single-threaded, same posture as `nexus-vault`: search requests
/// are IO-bound (kernel-syscall walk) and heavy work runs on the
/// blocking pool, so parallelism gain does not justify pulling
/// `rt-multi-thread` into the plugin's dep tree.  Build errors are
/// effectively unreachable on healthy hosts (OS-level thread creation
/// failure); a panic here at load time is the same posture
/// `nexus-vault` takes — a broken plugin is worse than a loud failure.
fn build_runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        // #4725: tokio's default thread name is "tokio-rt-worker",
        // which reads as a HOST thread in a panic line and sent the
        // investigation the wrong way.  Name ours.
        .thread_name("nexus-search-plugin")
        .thread_keep_alive(BLOCKING_THREAD_KEEP_ALIVE)
        .build()
        .expect("build search-plugin tokio runtime")
}

/// Plugin-ABI dispatch entry, wrapped in the last line of defence
/// (#4725).  A panic escaping a handler here would unwind into the
/// host's `extern "C"` `nexus_service_dispatch` and abort the WHOLE
/// nexusd-cluster process — kernel, raft, every plugin — for one
/// request's worth of trouble.  The realistic source is tokio
/// refusing a blocking thread (`OS can't spawn worker thread`), which
/// fires BEFORE any handler work runs, so nothing is left half-
/// mutated; the deeper stores are transactional regardless (FTS
/// commit probe, fail-closed dirty marks, `parking_lot` guards that
/// release on unwind).  tokio's current-thread scheduler hands its
/// core back from the `CoreGuard` drop when a `block_on` future
/// panics, so the runtime keeps serving afterwards.  Fail THIS RPC
/// with `Internal`, count it on Health, carry on.
fn dispatch_search(plugin: &SearchPlugin, method: &str, payload: &[u8]) -> Result<Vec<u8>, i32> {
    let guarded = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        dispatch_search_inner(plugin, method, payload)
    }));
    match guarded {
        Ok(result) => result,
        Err(payload) => {
            let reason = crate::http_client::panic_reason(payload.as_ref());
            tracing::error!(
                method = %method,
                reason = %reason,
                "nexus-search-plugin: handler panicked — failing this RPC instead of the host process",
            );
            plugin.svc.record_dispatch_panic(method, &reason);
            Err(-3 /* PluginResult::Internal */)
        }
    }
}

/// Legacy short-name Call RPC dispatch is intentionally empty — every
/// caller reaches us through Phase P gRPC routing.  The plugin ABI
/// still requires a dispatch function pointer, so we translate any
/// `/`-prefixed method into `dispatch_grpc` and reject the rest.
fn dispatch_search_inner(
    plugin: &SearchPlugin,
    method: &str,
    payload: &[u8],
) -> Result<Vec<u8>, i32> {
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
        // ── P8 Python-parity RPCs ───────────────────────────
        //
        // The dispatch macro pattern repeats; a helper
        // (dispatch_rpc!(name, req_ty, method)) would DRY it up
        // but the current 15 arms are still tractable and the
        // repetition is the code being self-documenting about
        // which RPCs land here.  Extract when a 20th arm shows
        // up.
        "BatchQuery" => {
            let req = BatchQueryRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "BatchQuery decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.batch_query(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "BatchQuery handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "IndexDocuments" => {
            let req = IndexDocumentsRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "IndexDocuments decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.index_documents(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "IndexDocuments handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "NotifyFileChange" => {
            let req = NotifyFileChangeRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "NotifyFileChange decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.notify_file_change(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "NotifyFileChange handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Locate" => {
            let req = LocateRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Locate decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.locate(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Locate handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "ParkedList" => {
            let req = ParkedListRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "ParkedList decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.parked_list(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "ParkedList handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "ParkedRetry" => {
            let req = ParkedRetryRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "ParkedRetry decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.parked_retry(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "ParkedRetry handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "ParkedDiscard" => {
            let req = ParkedDiscardRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "ParkedDiscard decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.parked_discard(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "ParkedDiscard handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "AddIndexedDirectory" => {
            let req = AddIndexedDirectoryRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "AddIndexedDirectory decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.add_indexed_directory(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "AddIndexedDirectory handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "RemoveIndexedDirectory" => {
            let req = RemoveIndexedDirectoryRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "RemoveIndexedDirectory decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.remove_indexed_directory(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "RemoveIndexedDirectory handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "ListIndexedDirectories" => {
            let req = ListIndexedDirectoriesRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "ListIndexedDirectories decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.list_indexed_directories(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "ListIndexedDirectories handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "SetZoneIndexingMode" => {
            let req = SetZoneIndexingModeRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "SetZoneIndexingMode decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.set_zone_indexing_mode(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "SetZoneIndexingMode handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "ListZoneIndexingModes" => {
            let req = ListZoneIndexingModesRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "ListZoneIndexingModes decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.list_zone_indexing_modes(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "ListZoneIndexingModes handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Health" => {
            let req = HealthRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Health decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.health(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Health handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        "Stats" => {
            let req = StatsRequest::decode(payload).map_err(|e| {
                tracing::warn!(err = %e, "Stats decode");
                -2
            })?;
            let resp = plugin
                .rt
                .block_on(plugin.svc.stats(Request::new(req)))
                .map_err(|s| {
                    tracing::warn!(status = %s, "Stats handler");
                    -3
                })?
                .into_inner();
            Ok(resp.encode_to_vec())
        }
        // Test seam (#4725): a panic INSIDE `block_on` — the shape a
        // thread-spawn failure in `spawn_blocking` takes — so the
        // dispatch-boundary guard and the runtime's recovery are
        // exercised end to end.
        #[cfg(test)]
        "__panic_for_test" => plugin
            .rt
            .block_on(async { panic!("injected dispatch panic") }),
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

#[cfg(test)]
mod tests {
    //! Dispatch-boundary guard (#4725).  A handler panic must fail its
    //! own RPC with `Internal`, be counted on Health, and leave the
    //! runtime serving — never unwind into the host's `extern "C"`
    //! dispatch (process abort).

    use std::ffi::c_void;
    use std::os::raw::c_char;

    use super::*;
    use crate::index_manager::IndexManager;
    use crate::search_proto::HealthResponse;

    // All-`-1` kernel: Health never touches the kernel; an accidental
    // touch fails loud instead of hitting a silent stub.
    unsafe extern "C" fn poison_buf(
        _: *const c_void,
        _: *const c_char,
        _: *mut *mut u8,
        _: *mut usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn poison_write(
        _: *const c_void,
        _: *const c_char,
        _: *const u8,
        _: usize,
    ) -> i32 {
        -1
    }
    unsafe extern "C" fn poison_path(_: *const c_void, _: *const c_char) -> i32 {
        -1
    }
    unsafe extern "C" fn poison_rename(
        _: *const c_void,
        _: *const c_char,
        _: *const c_char,
    ) -> i32 {
        -1
    }

    fn poison_handle() -> KernelHandle {
        KernelHandle {
            sys_read: poison_buf,
            sys_write: poison_write,
            sys_stat: poison_buf,
            sys_readdir: poison_buf,
            sys_unlink: poison_path,
            sys_mkdir: poison_path,
            sys_rmdir: poison_path,
            sys_rename: poison_rename,
            sys_stat_batch: poison_buf,
            free_buf: nexus_plugin_abi::nexus_free,
            kernel_ptr: std::ptr::null(),
        }
    }

    fn plugin_for_test() -> SearchPlugin {
        let root = tempfile::tempdir().expect("tempdir").keep();
        let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
            .manager(Arc::new(IndexManager::with_root(root)))
            .no_expander()
            .no_peer_fanout()
            .no_context_generator()
            .build();
        SearchPlugin {
            svc: Arc::new(svc),
            rt: build_runtime(),
        }
    }

    fn health(plugin: &SearchPlugin) -> HealthResponse {
        let bytes = dispatch_search(
            plugin,
            "/nexus.search.v1.SearchService/Health",
            &HealthRequest::default().encode_to_vec(),
        )
        .expect("health dispatch");
        HealthResponse::decode(bytes.as_slice()).expect("decode health")
    }

    #[test]
    fn handler_panic_fails_the_rpc_not_the_process_and_is_counted() {
        let plugin = plugin_for_test();
        assert_eq!(health(&plugin).dispatch_panics, 0);

        let code = dispatch_search(
            &plugin,
            "/nexus.search.v1.SearchService/__panic_for_test",
            &[],
        )
        .expect_err("a panicking handler fails its RPC");
        assert_eq!(code, -3, "PluginResult::Internal");

        // The runtime survived the panic inside `block_on` and the
        // service still answers; the panic is on the record.
        let h = health(&plugin);
        assert_eq!(h.dispatch_panics, 1);
        assert!(h.detail.contains("injected dispatch panic"), "{}", h.detail);
        assert_eq!(
            h.status, "degraded",
            "no embedder ⇒ degraded; the caught panic itself does not move status"
        );
        assert_eq!(
            health(&plugin).dispatch_panics,
            1,
            "counted per panic, not per poll"
        );
    }
}
