//! Cross-allocator FFI regression — nexus-vfs#236.
//!
//! The cluster HOST links **mimalloc** as its global allocator; a plugin
//! cdylib links the **system** allocator. Any heap buffer that crosses
//! the plugin C-ABI boundary must be freed by the side that ALLOCATED
//! it, or the free lands on the wrong heap. On Windows that is a hard
//! segfault (`HeapFree` rejects a mimalloc pointer); the Linux Docker
//! search-plugin E2E masks it (a foreign `free` there defers the
//! corruption instead of trapping), so it is NOT a faithful guard.
//!
//! This test reproduces the real thing without Docker: it sets mimalloc
//! as THIS binary's global allocator (playing the cluster host), dlopens
//! the REAL search-plugin cdylib (system allocator), wires a `KernelHandle`
//! whose `sys_*` callbacks allocate buffers HERE (mimalloc) and whose
//! `free_buf` frees them HERE (mimalloc), then drives a live `Grep`:
//!
//!   * every `sys_read`/`sys_readdir` the plugin makes hands it a
//!     mimalloc buffer it must free through `handle.free_buf` (host) —
//!     before the fix it freed them with its own `nexus_free` (system)
//!     and this test SEGFAULTED on Windows;
//!   * the `Grep` response is a system-allocated buffer the host frees
//!     through the plugin's exported `nexus_free` (plugin allocator).
//!
//! A clean pass (match found, process alive) proves both directions of
//! the ownership rule hold across two real allocators.

use std::collections::HashMap;
use std::ffi::{c_void, CStr, CString};
use std::os::raw::c_char;
use std::path::PathBuf;
use std::process::Command;

use nexus_plugin_abi::{KernelHandle, NexusFreeFn, ServiceCreateFn, ServiceDispatchFn};
use nexus_search_plugin::search_proto::{GrepRequest, GrepResponse};
use prost::Message;

// The whole point: a DIFFERENT global allocator than the system one the
// cdylib links, so a cross-boundary free is a genuine cross-heap free.
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[cfg(target_os = "linux")]
const PLUGIN_FILE: &str = "libnexus_search_plugin.so";
#[cfg(target_os = "macos")]
const PLUGIN_FILE: &str = "libnexus_search_plugin.dylib";
#[cfg(target_os = "windows")]
const PLUGIN_FILE: &str = "nexus_search_plugin.dll";

fn plugin_path() -> PathBuf {
    let exe = std::env::current_exe().expect("current_exe");
    let target_profile_dir = exe
        .parent()
        .and_then(|p| p.parent())
        .unwrap_or_else(|| panic!("unexpected test binary layout: {}", exe.display()));
    target_profile_dir.join(PLUGIN_FILE)
}

fn ensure_cdylib_built(plugin: &std::path::Path) {
    if plugin.exists() {
        return;
    }
    let mut cmd = Command::new(env!("CARGO"));
    cmd.args(["build", "-p", "nexus-search-plugin"]);
    if !cfg!(debug_assertions) {
        cmd.arg("--release");
    }
    let status = cmd.status().expect("invoke cargo");
    assert!(
        status.success(),
        "cargo build -p nexus-search-plugin failed"
    );
    assert!(plugin.exists(), "cdylib still not at {}", plugin.display());
}

// ── In-memory host VFS the plugin walks over the C-ABI ──────────────

struct HostFs {
    /// path → file bytes (regular files only).
    files: HashMap<String, Vec<u8>>,
    /// parent dir → children `(name, entry_type)` (0 = DT_REG, 1 = DT_DIR).
    dirs: HashMap<String, Vec<(String, u8)>>,
}

/// Hand a buffer to the plugin as an exact-capacity boxed slice — this
/// allocation happens in the TEST binary (mimalloc), and `host_free_buf`
/// (also mimalloc) is what the plugin must call back to release it.
unsafe fn yield_buf(bytes: Vec<u8>, out_buf: *mut *mut u8, out_len: *mut usize) {
    let boxed = bytes.into_boxed_slice();
    *out_len = boxed.len();
    *out_buf = Box::into_raw(boxed) as *mut u8;
}

unsafe extern "C" fn host_free_buf(ptr: *mut u8, len: usize) {
    if !ptr.is_null() && len > 0 {
        drop(Vec::from_raw_parts(ptr, len, len));
    }
}

unsafe extern "C" fn host_sys_read(
    k: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let fs = &*(k as *const HostFs);
    let Ok(p) = CStr::from_ptr(path).to_str() else {
        return -2;
    };
    match fs.files.get(p) {
        Some(bytes) => {
            yield_buf(bytes.clone(), out_buf, out_len);
            0
        }
        None => -1,
    }
}

unsafe extern "C" fn host_sys_readdir(
    k: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let fs = &*(k as *const HostFs);
    let Ok(p) = CStr::from_ptr(path).to_str() else {
        return -2;
    };
    match fs.dirs.get(p) {
        Some(entries) => {
            // The kernel's readdir JSON shape: [{"name":..,"entry_type":..}].
            let mut json = String::from("[");
            for (i, (name, et)) in entries.iter().enumerate() {
                if i > 0 {
                    json.push(',');
                }
                json.push_str(&format!("{{\"name\":\"{name}\",\"entry_type\":{et}}}"));
            }
            json.push(']');
            yield_buf(json.into_bytes(), out_buf, out_len);
            0
        }
        None => -1,
    }
}

unsafe extern "C" fn host_sys_stat(
    k: *const c_void,
    path: *const c_char,
    out_buf: *mut *mut u8,
    out_len: *mut usize,
) -> i32 {
    let fs = &*(k as *const HostFs);
    let Ok(p) = CStr::from_ptr(path).to_str() else {
        return -2;
    };
    let json = if let Some(bytes) = fs.files.get(p) {
        format!(
            r#"{{"path":"{p}","entry_type":0,"size":{},"zone_id":"root","modified_at_ms":0}}"#,
            bytes.len()
        )
    } else if fs.dirs.contains_key(p) {
        format!(
            r#"{{"path":"{p}","entry_type":1,"size":0,"zone_id":"root","modified_at_ms":null}}"#
        )
    } else {
        return -1;
    };
    yield_buf(json.into_bytes(), out_buf, out_len);
    0
}

// grep never invokes these — poison to -1 so a stray call fails loud.
unsafe extern "C" fn stub_write(_: *const c_void, _: *const c_char, _: *const u8, _: usize) -> i32 {
    -1
}
unsafe extern "C" fn stub_path(_: *const c_void, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn stub_rename(_: *const c_void, _: *const c_char, _: *const c_char) -> i32 {
    -1
}
unsafe extern "C" fn stub_stat_batch(
    _: *const c_void,
    _: *const c_char,
    _: *mut *mut u8,
    _: *mut usize,
) -> i32 {
    -1
}

fn handle_for(fs: *const HostFs) -> KernelHandle {
    KernelHandle {
        sys_read: host_sys_read,
        sys_write: stub_write,
        sys_stat: host_sys_stat,
        sys_readdir: host_sys_readdir,
        sys_unlink: stub_path,
        sys_mkdir: stub_path,
        sys_rmdir: stub_path,
        sys_rename: stub_rename,
        sys_stat_batch: stub_stat_batch,
        free_buf: host_free_buf,
        kernel_ptr: fs as *const c_void,
    }
}

#[test]
fn grep_over_cdylib_survives_cross_allocator_free() {
    let plugin = plugin_path();
    ensure_cdylib_built(&plugin);

    let lib = unsafe { libloading::Library::new(&plugin) }
        .unwrap_or_else(|e| panic!("dlopen({}) failed: {e}", plugin.display()));

    // Seed a tiny VFS: one file under root whose content the grep matches.
    let mut files = HashMap::new();
    files.insert(
        "/notes.txt".to_string(),
        b"alpha beta\ngamma NEEDLE delta\nepsilon\n".to_vec(),
    );
    let mut dirs = HashMap::new();
    dirs.insert("/".to_string(), vec![("notes.txt".to_string(), 0u8)]);
    // Box so the pointer stays stable + valid for the plugin's lifetime.
    let fs = Box::new(HostFs { files, dirs });
    let handle = handle_for(&*fs as *const HostFs);

    unsafe {
        let create: libloading::Symbol<ServiceCreateFn> = lib
            .get(b"nexus_service_create")
            .expect("resolve nexus_service_create");
        let dispatch: libloading::Symbol<ServiceDispatchFn> = lib
            .get(b"nexus_service_dispatch")
            .expect("resolve nexus_service_dispatch");
        let destroy: libloading::Symbol<unsafe extern "C" fn(*mut c_void)> = lib
            .get(b"nexus_service_destroy")
            .expect("resolve nexus_service_destroy");
        // The plugin's OWN free — the host uses it to release the
        // plugin-allocated dispatch response on the plugin's allocator.
        let plugin_free: libloading::Symbol<NexusFreeFn> =
            lib.get(b"nexus_free").expect("resolve nexus_free");

        let svc = create(&handle as *const KernelHandle);
        assert!(!svc.is_null(), "nexus_service_create returned null");

        // Drive a real Grep. Internally the plugin walks the VFS via our
        // mimalloc-allocating sys_readdir / sys_read and frees each buffer
        // through handle.free_buf — the exact path that segfaulted on
        // Windows before the fix.
        let req = GrepRequest {
            root_path: "/".to_string(),
            pattern: "NEEDLE".to_string(),
            ..Default::default()
        };
        let payload = req.encode_to_vec();
        let method = CString::new("/nexus.search.v1.SearchService/Grep").unwrap();

        let mut out_buf: *mut u8 = std::ptr::null_mut();
        let mut out_len: usize = 0;
        let rc = dispatch(
            svc,
            method.as_ptr(),
            payload.as_ptr(),
            payload.len(),
            &mut out_buf,
            &mut out_len,
        );
        assert_eq!(rc, 0, "Grep dispatch returned error code {rc}");

        // Copy the plugin-allocated response out (into a mimalloc Vec),
        // then free the plugin buffer via the plugin's OWN allocator.
        let resp_bytes = std::slice::from_raw_parts(out_buf, out_len).to_vec();
        plugin_free(out_buf, out_len);

        let resp = GrepResponse::decode(&resp_bytes[..]).expect("decode GrepResponse");
        assert!(
            resp.matches
                .iter()
                .any(|m| m.path == "/notes.txt" && m.line.contains("NEEDLE")),
            "grep did not surface the seeded match; got {:?}",
            resp.matches,
        );

        destroy(svc);
    }

    // Keep the host VFS alive until after destroy — the plugin held a
    // copy of the handle pointing at it for the whole dispatch.
    drop(fs);
}
