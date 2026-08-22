//! Shared test scaffold for the search-plugin integration tests.
//!
//! Two kernel doubles, formerly copy-pasted into every `*_e2e.rs`:
//!
//!   * [`MockKernel`] + [`handle_for`] — an in-memory VFS the walker
//!     (Index / Grep / Glob) reads through the real C-ABI callbacks.
//!   * [`poison_handle`] — an all-`-1` `KernelHandle` for the query /
//!     macro / peer tests, which never touch the kernel; an accidental
//!     touch fails loud instead of hitting a silent stub.
//!
//! The per-test `Harness` (service wiring — embedder, peer registry,
//! gRPC server) stays in each file: it is genuinely test-specific.
//!
//! `#![allow(dead_code)]`: each test binary `mod common;`-includes the
//! whole module but uses only the double it needs, so the other is
//! legitimately unused per-binary.
#![allow(dead_code)]

use std::collections::HashMap;
use std::ffi::{c_void, CStr};
use std::os::raw::c_char;

use nexus_plugin_abi::KernelHandle;

// ── MockKernel: an in-memory VFS behind the C-ABI callbacks ─────────

struct FileEntry {
    bytes: Vec<u8>,
    mtime_ms: i64,
}

/// In-memory kernel: `files` are regular-file/stream contents, `dirs`
/// maps a parent path to its `(name, entry_type)` children.
pub struct MockKernel {
    files: HashMap<String, FileEntry>,
    dirs: HashMap<String, Vec<(String, u8)>>,
}

impl MockKernel {
    pub fn new() -> Self {
        Self {
            files: HashMap::new(),
            dirs: HashMap::new(),
        }
    }

    pub fn add_file(&mut self, path: &str, bytes: &[u8], mtime_ms: i64) {
        self.files.insert(
            path.to_string(),
            FileEntry {
                bytes: bytes.to_vec(),
                mtime_ms,
            },
        );
        let (parent, name) = split_parent(path);
        self.dirs
            .entry(parent)
            .or_default()
            .push((name, 0 /* DT_REG */));
    }

    /// Register a DT_STREAM (entry_type 4) whose `sys_read` returns its WHOLE
    /// collected log — the host C-ABI shim's contract (nexus-vfs#235). To the
    /// plugin a stream is just another path-addressed document, so it indexes +
    /// embeds identically to a file; only the readdir entry_type differs, which
    /// is what the plugin's `searchable_content_type` gate keys off.
    pub fn add_stream(&mut self, path: &str, bytes: &[u8], mtime_ms: i64) {
        self.files.insert(
            path.to_string(),
            FileEntry {
                bytes: bytes.to_vec(),
                mtime_ms,
            },
        );
        let (parent, name) = split_parent(path);
        self.dirs
            .entry(parent)
            .or_default()
            .push((name, 4 /* DT_STREAM */));
    }

    pub fn add_dir(&mut self, path: &str) {
        if !self.dirs.contains_key(path) {
            self.dirs.insert(path.to_string(), Vec::new());
        }
        if path == "/" {
            return;
        }
        let (parent, name) = split_parent(path);
        let entries = self.dirs.entry(parent).or_default();
        if !entries.iter().any(|(n, _)| n == &name) {
            entries.push((name, 1 /* DT_DIR */));
        }
    }
}

/// Split `/a/b/c.md` into (`/a/b`, `c.md`).  `/foo` yields (`/`, `foo`).
fn split_parent(path: &str) -> (String, String) {
    match path.rsplit_once('/') {
        Some(("", name)) => ("/".to_string(), name.to_string()),
        Some((parent, name)) => (parent.to_string(), name.to_string()),
        None => ("/".to_string(), path.to_string()),
    }
}

/// Hand a Vec to the plugin as a `cap == len` buffer + `mem::forget` it,
/// matching the plugin's `free_buf`/`nexus_free` reconstruction contract.
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

unsafe extern "C" fn mock_sys_read(
    k: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(path).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    match kernel.files.get(p) {
        Some(entry) => {
            into_out_buf(entry.bytes.clone(), out_buf, out_len);
            0
        }
        None => -404, // NotFound sentinel per plugin-ABI convention
    }
}

unsafe extern "C" fn mock_sys_readdir(
    k: *const c_void,
    parent_path: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(parent_path).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    match kernel.dirs.get(p) {
        Some(entries) => {
            // JSON encoded per the plugin ABI: [{"name":str,"entry_type":u8}].
            let payload: Vec<serde_json::Value> = entries
                .iter()
                .map(|(name, et)| serde_json::json!({ "name": name, "entry_type": et }))
                .collect();
            let json = serde_json::to_vec(&payload).expect("mock readdir json");
            into_out_buf(json, out_json, out_len);
            0
        }
        None => -404,
    }
}

unsafe extern "C" fn mock_sys_stat(
    k: *const c_void,
    path: *const c_char,
    out_json: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let kernel = &*(k as *const MockKernel);
    let p = match CStr::from_ptr(path).to_str() {
        Ok(s) => s,
        Err(_) => return -2,
    };
    if let Some(entry) = kernel.files.get(p) {
        let payload = serde_json::json!({
            "path": p,
            "entry_type": 0,
            "size": entry.bytes.len(),
            "zone_id": "root",
            "modified_at_ms": entry.mtime_ms,
        });
        into_out_buf(serde_json::to_vec(&payload).unwrap(), out_json, out_len);
        0
    } else if kernel.dirs.contains_key(p) {
        let payload = serde_json::json!({
            "path": p,
            "entry_type": 1,
            "size": 0,
            "zone_id": "root",
            "modified_at_ms": null,
        });
        into_out_buf(serde_json::to_vec(&payload).unwrap(), out_json, out_len);
        0
    } else {
        -404
    }
}

// The write-side callbacks are unused by the read/index walk but the
// plugin ABI requires the full vtable — return -1 so any accidental use
// fails loudly.
unsafe extern "C" fn unused_sys_write(
    _: *const c_void,
    _: *const c_char,
    _: *const u8,
    _: usize,
) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_unlink(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_mkdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_rmdir(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_rename(
    _: *const c_void,
    _: *const c_char,
    _: *const c_char,
) -> i32 {
    -1
}
unsafe extern "C" fn unused_sys_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

/// Build a `KernelHandle` whose read/readdir/stat resolve against `kernel`.
pub fn handle_for(kernel: *const MockKernel) -> KernelHandle {
    KernelHandle {
        sys_read: mock_sys_read,
        sys_write: unused_sys_write,
        sys_stat: mock_sys_stat,
        sys_readdir: mock_sys_readdir,
        sys_unlink: unused_sys_unlink,
        sys_mkdir: unused_sys_mkdir,
        sys_rmdir: unused_sys_rmdir,
        sys_rename: unused_sys_rename,
        sys_stat_batch: unused_sys_stat_batch,
        free_buf: nexus_plugin_abi::nexus_free,
        kernel_ptr: kernel as *const c_void,
    }
}

// ── Poison KernelHandle: all callbacks fail loud (-1) ──────────────
//
// For tests whose path never touches the kernel (Query, macro expand,
// peer fan-out).  If a regression wires the kernel back in, every
// callback returns -1 (plugin-ABI Internal) which surfaces as a
// zero-hit result — visible enough to fail the test's assertion.

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

/// An all-`-1` `KernelHandle` for tests that must never reach the kernel.
pub fn poison_handle() -> KernelHandle {
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
        free_buf: nexus_plugin_abi::nexus_free,
        kernel_ptr: std::ptr::null(),
    }
}
