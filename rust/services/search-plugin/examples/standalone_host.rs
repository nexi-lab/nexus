//! Standalone gRPC host for SearchServiceImpl — local validation only.
//!
//! Serves the real plugin service over tonic transport with a
//! directory-backed KernelHandle, so a Python nexus server container
//! (NEXUS_SEARCH_PLUGIN_TARGET=host.docker.internal:2126) can be
//! validated end-to-end without building the full nexusd-cluster
//! kernel. NOT part of the shipped plugin; never loaded as a cdylib.
//!
//! Env:
//!   HOST_CORPUS_DIR              — root dir backing sys_read/readdir/stat
//!   HOST_DATA_DIR                — index storage root
//!   HOST_BIND                    — listen addr (default 0.0.0.0:2126)
//!   NEXUS_SEARCH_MODEL_DIR       — mE5 model dir (fastembed layout)
//!   ORT_DYLIB_PATH               — onnxruntime dylib

use std::ffi::{c_void, CStr};
use std::os::raw::c_char;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use nexus_plugin_abi::KernelHandle;
use nexus_search_plugin::embedder::build_default_embedder;
use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchServiceServer;
use nexus_search_plugin::service::SearchServiceImpl;

struct DirKernel {
    root: PathBuf,
}

impl DirKernel {
    fn resolve(&self, p: &str) -> PathBuf {
        self.root.join(p.trim_start_matches('/'))
    }
}

fn into_out_buf(mut v: Vec<u8>, out_buf: *mut *mut u8, out_len: *mut usize) {
    v.shrink_to_fit();
    let len = v.len();
    let ptr = v.as_mut_ptr();
    std::mem::forget(v);
    unsafe {
        *out_buf = ptr;
        *out_len = len;
    }
}

unsafe fn cstr<'a>(p: *const c_char) -> Option<&'a str> {
    CStr::from_ptr(p).to_str().ok()
}

unsafe extern "C" fn sys_read(
    k: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const DirKernel);
    let Some(p) = cstr(path) else { return -2 };
    match std::fs::read(kernel.resolve(p)) {
        Ok(bytes) => {
            into_out_buf(bytes, out_buf, out_len);
            0
        }
        Err(_) => -404,
    }
}

unsafe extern "C" fn sys_readdir(
    k: *const c_void,
    parent: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const DirKernel);
    let Some(p) = cstr(parent) else { return -2 };
    let dir = kernel.resolve(p);
    let Ok(rd) = std::fs::read_dir(&dir) else {
        return -404;
    };
    let mut entries = Vec::new();
    for e in rd.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        let et: u8 = if e.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            1
        } else {
            0
        };
        entries.push(serde_json::json!({ "name": name, "entry_type": et }));
    }
    match serde_json::to_vec(&entries) {
        Ok(json) => {
            into_out_buf(json, out_json, out_len);
            0
        }
        Err(_) => -1,
    }
}

unsafe extern "C" fn sys_stat(
    k: *const c_void,
    path: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const DirKernel);
    let Some(p) = cstr(path) else { return -2 };
    let fs_path = kernel.resolve(p);
    let Ok(meta) = std::fs::metadata(&fs_path) else {
        return -404;
    };
    let mtime_ms = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_millis() as i64);
    let payload = serde_json::json!({
        "path": p,
        "entry_type": if meta.is_dir() { 1 } else { 0 },
        "size": meta.len(),
        "zone_id": "root",
        "modified_at_ms": mtime_ms,
    });
    match serde_json::to_vec(&payload) {
        Ok(json) => {
            into_out_buf(json, out_json, out_len);
            0
        }
        Err(_) => -1,
    }
}

unsafe extern "C" fn deny_write(
    _: *const c_void,
    _: *const c_char,
    _: *const u8,
    _: usize,
) -> i32 {
    -1
}
unsafe extern "C" fn deny_unlink(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn deny_mkdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn deny_rmdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn deny_rename(_: *const c_void, _: *const c_char, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn deny_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let corpus = std::env::var("HOST_CORPUS_DIR").unwrap_or_else(|_| "/tmp/corpus".into());
    let data = std::env::var("HOST_DATA_DIR").unwrap_or_else(|_| "/tmp/plugin-data".into());
    let bind = std::env::var("HOST_BIND").unwrap_or_else(|_| "0.0.0.0:2126".into());

    std::fs::create_dir_all(&corpus)?;
    std::fs::create_dir_all(&data)?;

    let kernel = Box::into_raw(Box::new(DirKernel {
        root: PathBuf::from(&corpus),
    }));
    let handle = KernelHandle {
        sys_read,
        sys_write: deny_write,
        sys_stat,
        sys_readdir,
        sys_unlink: deny_unlink,
        sys_mkdir: deny_mkdir,
        sys_rmdir: deny_rmdir,
        sys_rename: deny_rename,
        sys_stat_batch: deny_stat_batch,
        kernel_ptr: kernel as *const c_void,
    };

    let manager = Arc::new(IndexManager::with_root(PathBuf::from(&data)));

    // Real embedder, fail-loud: quality validation is pointless on the mock.
    let embedder = build_default_embedder(Path::new(&data))?;
    eprintln!(
        "standalone host: embedder tag={} dim={}",
        embedder.tag(),
        embedder.dim()
    );

    let svc = SearchServiceImpl::builder(Arc::new(handle))
        .manager(manager)
        .embedder(embedder)
        .build();

    let addr = bind.parse()?;
    eprintln!("standalone host: serving SearchService on {addr} (corpus={corpus} data={data})");
    tonic::transport::Server::builder()
        .add_service(SearchServiceServer::new(svc))
        .serve(addr)
        .await?;
    Ok(())
}
