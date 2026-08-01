//! Thin safe wrappers around the raw C-ABI `KernelHandle` callbacks.
//!
//! Every syscall the search walker needs (`sys_readdir` + `sys_read`)
//! is exposed here as a Rust-ergonomic `Result<T, KernelIoError>`
//! function.  Keeps the `unsafe extern "C"` FFI + `nexus_free`
//! bookkeeping in one place so the walker / grep loop stays in
//! straight-line Rust.

use std::ffi::{CString, NulError};
use std::slice;

use nexus_plugin_abi::{nexus_free, KernelHandle};
use serde::Deserialize;

/// Errors surfaced from the kernel-side callbacks.  Mirrors the
/// non-zero return codes documented on `KernelHandle` per syscall
/// plus a couple of Rust-side wrapper errors (`NulError`, JSON parse).
#[derive(Debug)]
pub enum KernelIoError {
    /// The path contained an interior NUL — cannot be handed to a
    /// C-ABI `*const c_char`.
    PathHasNul,
    /// `sys_readdir` / `sys_read` returned `PluginResult::NotFound`.
    NotFound,
    /// Non-`NotFound` non-zero return.  Value is the raw i32 the
    /// callback produced.
    Kernel(i32),
    /// `sys_readdir` returned bytes that did not parse as
    /// `[{"name":str,"entry_type":u8}]`.
    JsonParse(serde_json::Error),
    /// `sys_read` returned bytes we could not interpret as UTF-8
    /// (grep is line-oriented — binary files are skipped upstream).
    NotUtf8,
}

impl From<NulError> for KernelIoError {
    fn from(_: NulError) -> Self {
        Self::PathHasNul
    }
}

/// Single entry from `sys_readdir`.
///
/// Field names mirror the JSON payload the plugin-ABI docs on
/// `sys_readdir` promise: `{"name":<str>,"entry_type":<u8>}`.
#[derive(Debug, Deserialize)]
pub struct DirEntry {
    pub name: String,
    #[serde(rename = "entry_type")]
    pub entry_type: u8,
}

// `entry_type` constants — match `kernel::meta_store::DT_*` in
// nexus-vfs.  Kept as `const u8` (not an enum) so a future kernel
// bump adding a new DT_ variant does not require a plugin-side
// exhaustive-match update.
pub const DT_REG: u8 = 0;
pub const DT_DIR: u8 = 1;
#[allow(dead_code)]
pub const DT_MOUNT: u8 = 2;

/// Wrap `handle.sys_readdir(path)`.
///
/// Returns the parsed entry list on success; propagates NotFound so
/// the walker can distinguish "dead entry" from "walk error".
pub fn sys_readdir(handle: &KernelHandle, path: &str) -> Result<Vec<DirEntry>, KernelIoError> {
    let c_path = CString::new(path)?;
    let mut out_ptr: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    // SAFETY: `sys_readdir` is documented on `KernelHandle` as taking
    // a null-terminated path and populating `(*out_ptr, *out_len)`
    // with a `nexus_free`-owned buffer on Ok(0).  We own the pointer
    // targets on the Rust stack; the callback writes into them via
    // `*mut *mut u8` / `*mut usize`.
    let rc = unsafe {
        (handle.sys_readdir)(
            handle.kernel_ptr,
            c_path.as_ptr(),
            &mut out_ptr as *mut *mut u8,
            &mut out_len as *mut usize,
        )
    };
    match rc {
        0 => {
            // SAFETY: on Ok, the callback populated `out_ptr` with a
            // heap-allocated buffer of `out_len` bytes; we borrow it
            // for the parse then hand it back to `nexus_free`.
            let bytes = unsafe { slice::from_raw_parts(out_ptr, out_len) };
            let parsed =
                serde_json::from_slice::<Vec<DirEntry>>(bytes).map_err(KernelIoError::JsonParse);
            // Free regardless of parse outcome — kernel owns the alloc.
            unsafe { nexus_free(out_ptr, out_len) };
            parsed
        }
        -1 => Err(KernelIoError::NotFound),
        other => Err(KernelIoError::Kernel(other)),
    }
}

/// Wrap `handle.sys_read(path)`.
///
/// Returns the file bytes as an owned `Vec<u8>`.  Zero-copy is not
/// worth it here — the walker immediately UTF-8-decodes for line
/// scanning, so an intermediate copy costs the same as the decode.
pub fn sys_read(handle: &KernelHandle, path: &str) -> Result<Vec<u8>, KernelIoError> {
    let c_path = CString::new(path)?;
    let mut out_ptr: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    // SAFETY: same shape as `sys_readdir` above — kernel-side callback
    // owns the alloc, we take a snapshot into an owned Vec and free.
    let rc = unsafe {
        (handle.sys_read)(
            handle.kernel_ptr,
            c_path.as_ptr(),
            &mut out_ptr as *mut *mut u8,
            &mut out_len as *mut usize,
        )
    };
    match rc {
        0 => {
            let bytes = unsafe { slice::from_raw_parts(out_ptr, out_len) };
            let owned = bytes.to_vec();
            unsafe { nexus_free(out_ptr, out_len) };
            Ok(owned)
        }
        -1 => Err(KernelIoError::NotFound),
        other => Err(KernelIoError::Kernel(other)),
    }
}

/// Subset of `StatResult`'s JSON shape that the plugin walker cares
/// about.  Serde ignores unknown fields (`entry_type`, `size`,
/// `zone_id`, `path`, …) so this stays additive-compatible with any
/// future schema growth on the kernel side.  `modified_at_ms` is
/// `null`-safe: older kernels that predate the field return it as
/// `null` and the walker degrades to "unknown mtime, sort last".
#[derive(Debug, Deserialize)]
pub struct StatInfo {
    pub modified_at_ms: Option<i64>,
}

/// Wrap `handle.sys_stat(path)`.
///
/// Returns just the field(s) the search walker uses today
/// (`modified_at_ms`).  Anything else in the JSON is left to serde's
/// default field-drop behavior so extending the kernel-side schema
/// stays wire-compatible.
pub fn sys_stat(handle: &KernelHandle, path: &str) -> Result<StatInfo, KernelIoError> {
    let c_path = CString::new(path)?;
    let mut out_ptr: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    // SAFETY: same shape as `sys_read` / `sys_readdir` above — kernel-side
    // callback owns the alloc; we take a snapshot then free.
    let rc = unsafe {
        (handle.sys_stat)(
            handle.kernel_ptr,
            c_path.as_ptr(),
            &mut out_ptr as *mut *mut u8,
            &mut out_len as *mut usize,
        )
    };
    match rc {
        0 => {
            let bytes = unsafe { slice::from_raw_parts(out_ptr, out_len) };
            let parsed: Result<StatInfo, _> = serde_json::from_slice(bytes);
            unsafe { nexus_free(out_ptr, out_len) };
            parsed.map_err(KernelIoError::JsonParse)
        }
        -1 => Err(KernelIoError::NotFound),
        other => Err(KernelIoError::Kernel(other)),
    }
}

/// Concatenate a parent VFS path and a child entry name into a
/// canonical child path.  Handles the root special case (`/` +
/// `foo` = `/foo`) and strips a redundant trailing `/` on the
/// parent.
pub fn join_vfs_path(parent: &str, child: &str) -> String {
    if parent == "/" {
        format!("/{child}")
    } else {
        let trimmed = parent.trim_end_matches('/');
        format!("{trimmed}/{child}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn join_root() {
        assert_eq!(join_vfs_path("/", "foo"), "/foo");
    }

    #[test]
    fn join_deep() {
        assert_eq!(join_vfs_path("/a/b", "c"), "/a/b/c");
        assert_eq!(join_vfs_path("/a/b/", "c"), "/a/b/c");
    }
}
