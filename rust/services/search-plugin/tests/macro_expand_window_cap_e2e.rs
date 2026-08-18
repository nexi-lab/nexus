//! E2E cover for `NEXUS_SEARCH_MACRO_EXPAND_WINDOW` env knob —
//! pins that `MacroExpandConfig::from_env` actually feeds
//! `window_for_anchor`, so an operator narrowing the window
//! gets exactly what they asked for at the wire level.
//!
//! Sibling of `macro_expand_e2e.rs` — split into a separate
//! integration binary because Cargo runs each `tests/*.rs` in its
//! own process; the env var this test sets would leak into a
//! sibling test in the same file that reads the same env at a
//! different value.  One env-mutating test per file keeps env
//! isolation without hand-rolling `serial_test` or a mutex.

use std::ffi::c_void;
use std::os::raw::c_char;
use std::sync::Arc;

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{QueryRequest, QueryType};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

// ── Poison KernelHandle (Query does not touch the kernel) ───────

unsafe extern "C" fn poison_read(
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
unsafe extern "C" fn poison_stat(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}
unsafe extern "C" fn poison_readdir(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}
unsafe extern "C" fn poison_unlink(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_mkdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_rmdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_rename(_: *const c_void, _: *const c_char, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn poison_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

fn poison_handle() -> KernelHandle {
    KernelHandle {
        sys_read: poison_read,
        sys_write: poison_write,
        sys_stat: poison_stat,
        sys_readdir: poison_readdir,
        sys_unlink: poison_unlink,
        sys_mkdir: poison_mkdir,
        sys_rmdir: poison_rmdir,
        sys_rename: poison_rename,
        sys_stat_batch: poison_stat_batch,
        kernel_ptr: std::ptr::null(),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expand_macro_respects_window_env_cap() {
    unsafe {
        std::env::set_var("NEXUS_SEARCH_MACRO_EXPAND_WINDOW", "1");
    }
    // Reset regardless of assert outcome so a later re-run in the
    // same session (`cargo test` retries) starts from a clean env.
    struct EnvReset;
    impl Drop for EnvReset {
        fn drop(&mut self) {
            unsafe {
                std::env::remove_var("NEXUS_SEARCH_MACRO_EXPAND_WINDOW");
            }
        }
    }
    let _guard = EnvReset;

    let dir = TempDir::new().expect("tempdir");
    let manager = Arc::new(IndexManager::with_root(dir.path().to_path_buf()));
    let svc = SearchServiceImpl::builder(Arc::new(poison_handle()))
        .manager(Arc::clone(&manager))
        .build();

    // Seed 10 chunks under one path; chunk 3 carries "needle" so
    // the BM25 hit lands there.
    let idx = manager.get_or_open("root").expect("open zone");
    for chunk_index in 0..10u32 {
        let text = if chunk_index == 3 {
            format!("body-{chunk_index} needle")
        } else {
            format!("body-{chunk_index}")
        };
        idx.add_document(
            "/notes/hit.md",
            chunk_index,
            &text,
            Some(chunk_index as i64),
        )
        .expect("add");
    }
    idx.commit().expect("commit");

    let resp = svc
        .query(Request::new(QueryRequest {
            q: "needle".into(),
            zone_id: "root".into(),
            limit: 5,
            query_type: QueryType::Keyword as i32,
            expand: "macro".into(),
            ..Default::default()
        }))
        .await
        .expect("query")
        .into_inner();

    assert!(resp.error.is_none(), "unexpected error: {:?}", resp.error);
    let hit = resp
        .results
        .iter()
        .find(|r| r.path == "/notes/hit.md")
        .expect("hit.md must be in results");
    let ctx = &hit.expanded_context;

    // With window=1 and anchor=3, only chunks 2, 3, 4 fit.  Chunks
    // 0-1 and 5-9 MUST NOT appear.  This is the whole point of the
    // window knob — an operator who wants a narrower context gets
    // exactly what they asked for.
    assert!(
        ctx.contains("body-2") && ctx.contains("body-3 needle") && ctx.contains("body-4"),
        "expanded_context must cover chunks 2/3/4; got:\n{ctx}",
    );
    for far_chunk in [0u32, 1, 5, 6, 7, 8, 9] {
        let far_body = format!("body-{far_chunk}");
        assert!(
            !ctx.contains(&far_body),
            "window=1 should have excluded chunk {far_chunk}; got:\n{ctx}",
        );
    }
}
