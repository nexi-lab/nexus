//! Thin safe wrappers around the `KernelHandle` C ABI callbacks,
//! shared by both the fuser-based [`crate::fs`] (Linux / macOS) and
//! the WinFsp-based [`crate::fs_winfsp`] (Windows) adapters.
//!
//! Each function takes `(&KernelHandle, path)` (plus payload bytes
//! for writes), translates Rust `&str` ↔ `CString`, dispatches
//! through the `extern "C"` fn pointer on the handle, and on success
//! takes ownership of the kernel-allocated out-buffer + frees it via
//! the handle's [`KernelHandle::free_buf`] callback (NOT the plugin's
//! own `nexus_free` — these buffers are HOST-allocated and the kernel
//! links a different global allocator; a cross-heap free segfaults on
//! Windows) before returning a Rust-side `String` / `Vec<u8>`.
//!
//! The returned `Err(i32)` is the raw C-ABI rc — mapping to a
//! platform-specific error code (Errno for fuser, NTSTATUS for
//! WinFsp) happens at the trait-implementation layer, not here.
//!
//! ## Why this module exists
//!
//! Before extraction these wrappers existed verbatim in both
//! adapter modules: ~120 lines duplicated.  Any time the kernel
//! widened the JSON shape returned by `sys_stat` or `sys_readdir`
//! both copies had to be updated in lockstep, and the parsers
//! ([`parse_stat`] / [`parse_readdir`]) duplicated another ~30
//! lines.  Single-SSOT module here so the kernel ABI is consumed in
//! exactly one place per syscall.

use std::ffi::CString;

use nexus_plugin_abi::{KernelHandle, NexusFreeFn};

/// Raw `i32` "I/O error" used when the kernel callback can't be
/// invoked at all (e.g. NUL in the path string).  Maps to
/// `libc::EIO` / `STATUS_INVALID_DEVICE_REQUEST` at the adapter
/// boundary — see the platform-specific `errno_to_*` functions.
pub const ERRNO_IO_RAW: i32 = -3;

/// Wrapper around the C `sys_stat` callback.  Returns the JSON
/// payload as a `String` on success.  The kernel's serializer shape
/// is documented under `kernel_cb_sys_stat` in
/// `nexus-vfs/rust/kernel/src/kernel/plugins/mod.rs`.
pub fn sys_stat(kernel: &KernelHandle, path: &str) -> Result<String, i32> {
    let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
    let mut out_buf: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    let rc = unsafe {
        (kernel.sys_stat)(
            kernel.kernel_ptr,
            c_path.as_ptr(),
            &mut out_buf,
            &mut out_len,
        )
    };
    if rc != 0 {
        return Err(rc);
    }
    consume_string_out(kernel.free_buf, out_buf, out_len)
}

/// Wrapper around the C `sys_read` callback.  Returns the full file
/// content as a `Vec<u8>`; callers slice for offset/size.
pub fn sys_read(kernel: &KernelHandle, path: &str) -> Result<Vec<u8>, i32> {
    let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
    let mut out_buf: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    let rc = unsafe {
        (kernel.sys_read)(
            kernel.kernel_ptr,
            c_path.as_ptr(),
            &mut out_buf,
            &mut out_len,
        )
    };
    if rc != 0 {
        return Err(rc);
    }
    consume_bytes_out(kernel.free_buf, out_buf, out_len)
}

/// Wrapper around the C `sys_write` callback.
pub fn sys_write(kernel: &KernelHandle, path: &str, data: &[u8]) -> Result<(), i32> {
    let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
    let rc = unsafe {
        (kernel.sys_write)(
            kernel.kernel_ptr,
            c_path.as_ptr(),
            data.as_ptr(),
            data.len(),
        )
    };
    if rc != 0 {
        return Err(rc);
    }
    Ok(())
}

/// Wrapper around the C `sys_readdir` callback.  Returns the JSON
/// array `[{"name":..,"entry_type":..}, ...]` as a `String`.
pub fn sys_readdir(kernel: &KernelHandle, parent_path: &str) -> Result<String, i32> {
    let c_path = CString::new(parent_path).map_err(|_| ERRNO_IO_RAW)?;
    let mut out_buf: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    let rc = unsafe {
        (kernel.sys_readdir)(
            kernel.kernel_ptr,
            c_path.as_ptr(),
            &mut out_buf,
            &mut out_len,
        )
    };
    if rc != 0 {
        return Err(rc);
    }
    consume_string_out(kernel.free_buf, out_buf, out_len)
}

/// Wrapper around the C `sys_unlink` callback.
pub fn sys_unlink(kernel: &KernelHandle, path: &str) -> Result<(), i32> {
    let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
    let rc = unsafe { (kernel.sys_unlink)(kernel.kernel_ptr, c_path.as_ptr()) };
    if rc != 0 {
        return Err(rc);
    }
    Ok(())
}

/// Wrapper around the C `sys_mkdir` callback.
pub fn sys_mkdir(kernel: &KernelHandle, path: &str) -> Result<(), i32> {
    let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
    let rc = unsafe { (kernel.sys_mkdir)(kernel.kernel_ptr, c_path.as_ptr()) };
    if rc != 0 {
        return Err(rc);
    }
    Ok(())
}

/// Wrapper around the C `sys_rmdir` callback.
pub fn sys_rmdir(kernel: &KernelHandle, path: &str) -> Result<(), i32> {
    let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
    let rc = unsafe { (kernel.sys_rmdir)(kernel.kernel_ptr, c_path.as_ptr()) };
    if rc != 0 {
        return Err(rc);
    }
    Ok(())
}

/// Wrapper around the C `sys_stat_batch` callback (v3+).  Takes a
/// slice of paths and returns a `Vec<Option<(size, entry_type)>>`
/// aligned with the input.  `None` slots correspond to paths the
/// kernel could not stat (the per-path `Option<StatResult>` in
/// `kernel::stat_batch`).
///
/// Replaces N × `sys_stat` round-trips with one FFI hop + one
/// kernel pass — used by the WinFsp adapter's `read_directory` to
/// populate `FileInfo.file_size` for every entry without paying
/// per-entry stat cost.  Linux/macOS FUSE traverses one entry at a
/// time so this batch path is Windows-only consumer territory —
/// hence the cfg-conditional dead-code allow.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
pub fn sys_stat_batch(
    kernel: &KernelHandle,
    paths: &[String],
) -> Result<Vec<Option<(u64, u64)>>, i32> {
    // Hand-roll the input JSON array of path strings — same minimal
    // pattern the kernel-side input parser handles, mirror of the
    // readdir output side (escape `\` and `"`).
    let mut input = String::from("[");
    let mut first = true;
    for p in paths {
        if !first {
            input.push(',');
        }
        first = false;
        input.push('"');
        for ch in p.chars() {
            match ch {
                '"' => input.push_str("\\\""),
                '\\' => input.push_str("\\\\"),
                c => input.push(c),
            }
        }
        input.push('"');
    }
    input.push(']');
    let c_input = CString::new(input).map_err(|_| ERRNO_IO_RAW)?;
    let mut out_buf: *mut u8 = std::ptr::null_mut();
    let mut out_len: usize = 0;
    let rc = unsafe {
        (kernel.sys_stat_batch)(
            kernel.kernel_ptr,
            c_input.as_ptr(),
            &mut out_buf,
            &mut out_len,
        )
    };
    if rc != 0 {
        return Err(rc);
    }
    let json = consume_string_out(kernel.free_buf, out_buf, out_len)?;
    Ok(parse_stat_batch_output(&json))
}

/// Parse the `sys_stat_batch` output JSON.  Shape per slot:
///
/// * `[<size>, <entry_type>]` → `Some((size, entry_type))`
/// * `null`                   → `None`
///
/// Returns an empty vec on malformed input — same fail-soft posture
/// as [`parse_readdir`].
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn parse_stat_batch_output(json: &str) -> Vec<Option<(u64, u64)>> {
    let mut out = Vec::new();
    // Strip exactly one outer `[ ... ]` pair — `trim_*_matches` would
    // chew through `[[` / `]]` at the boundaries with adjacent pairs.
    let trimmed = json.trim();
    let mut rest = trimmed
        .strip_prefix('[')
        .and_then(|s| s.strip_suffix(']'))
        .unwrap_or(trimmed);
    while !rest.is_empty() {
        rest = rest.trim_start_matches(',').trim_start();
        if let Some(after) = rest.strip_prefix("null") {
            out.push(None);
            rest = after;
            continue;
        }
        let Some(after_open) = rest.strip_prefix('[') else {
            return out;
        };
        let Some(size) = json_u64_at_head(after_open) else {
            return out;
        };
        // Walk past the `<size>,` to the entry_type.
        let after_size = match after_open.split_once(',') {
            Some((_, r)) => r.trim_start(),
            None => return out,
        };
        let Some(entry_type) = json_u64_at_head(after_size) else {
            return out;
        };
        // Walk to the closing `]` of this pair.
        let after_pair = match after_size.split_once(']') {
            Some((_, r)) => r,
            None => return out,
        };
        out.push(Some((size, entry_type)));
        rest = after_pair;
    }
    out
}

/// Parse a leading non-negative integer from `s`, returning `None`
/// when `s` doesn't start with digits.  Companion to [`json_u64`]
/// which scans for a keyed value; this one's for already-positioned
/// numeric literals.  Helper of [`parse_stat_batch_output`] — same
/// Windows-only callsite, same dead-code allow.
#[cfg_attr(not(target_os = "windows"), allow(dead_code))]
fn json_u64_at_head(s: &str) -> Option<u64> {
    let end = s.find(|c: char| !c.is_ascii_digit()).unwrap_or(s.len());
    if end == 0 {
        return None;
    }
    s[..end].parse().ok()
}

/// Wrapper around the C `sys_rename` callback.
pub fn sys_rename(kernel: &KernelHandle, old_path: &str, new_path: &str) -> Result<(), i32> {
    let c_old = CString::new(old_path).map_err(|_| ERRNO_IO_RAW)?;
    let c_new = CString::new(new_path).map_err(|_| ERRNO_IO_RAW)?;
    let rc = unsafe { (kernel.sys_rename)(kernel.kernel_ptr, c_old.as_ptr(), c_new.as_ptr()) };
    if rc != 0 {
        return Err(rc);
    }
    Ok(())
}

// ── async wrappers (spawn_blocking, macOS NFS only) ────────────────
//
// Each function copies the relevant function pointer + `kernel_ptr`
// into the closure, allocates `CString` arguments on the caller's
// async thread, then moves everything into `spawn_blocking` so the
// C FFI call runs on a blocking OS thread instead of a tokio worker.
//
// `kernel_ptr` (`*const c_void`) is not `Send`, but `KernelHandle`
// has `unsafe impl Send+Sync` — the kernel guarantees the pointer
// stays valid across threads.  We cast to `usize` to satisfy
// `spawn_blocking`'s `Send` bound, then cast back inside the
// closure.
//
// This prevents NFS async methods from starving the 2-thread tokio
// runtime when kernel callbacks take > 0 time (Raft consensus,
// redb I/O, etc.).
//
// Gated to macOS because the NFS adapter and tokio dep are macOS-only;
// Linux uses fuser (sync), Windows uses WinFsp (sync).
#[cfg(target_os = "macos")]
pub use nfs_async::*;

#[cfg(target_os = "macos")]
mod nfs_async {
    use super::*;

    /// Async wrapper around [`super::sys_stat`] — runs the C FFI call on a
    /// blocking thread via `tokio::task::spawn_blocking`.
    pub async fn sys_stat_async(kernel: &KernelHandle, path: &str) -> Result<String, i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_stat;
        let free_buf = kernel.free_buf;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let mut out_buf: *mut u8 = std::ptr::null_mut();
            let mut out_len: usize = 0;
            let rc = unsafe { (fn_ptr)(kptr, c_path.as_ptr(), &mut out_buf, &mut out_len) };
            if rc != 0 {
                return Err(rc);
            }
            consume_string_out(free_buf, out_buf, out_len)
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_read`].
    pub async fn sys_read_async(kernel: &KernelHandle, path: &str) -> Result<Vec<u8>, i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_read;
        let free_buf = kernel.free_buf;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let mut out_buf: *mut u8 = std::ptr::null_mut();
            let mut out_len: usize = 0;
            let rc = unsafe { (fn_ptr)(kptr, c_path.as_ptr(), &mut out_buf, &mut out_len) };
            if rc != 0 {
                return Err(rc);
            }
            consume_bytes_out(free_buf, out_buf, out_len)
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_write`].
    pub async fn sys_write_async(
        kernel: &KernelHandle,
        path: &str,
        data: &[u8],
    ) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_write;
        let kptr = kernel.kernel_ptr as usize;
        // Copy data into an owned Vec so it can move into the closure.
        let owned_data = data.to_vec();
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let rc =
                unsafe { (fn_ptr)(kptr, c_path.as_ptr(), owned_data.as_ptr(), owned_data.len()) };
            if rc != 0 {
                return Err(rc);
            }
            Ok(())
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_readdir`].
    pub async fn sys_readdir_async(
        kernel: &KernelHandle,
        parent_path: &str,
    ) -> Result<String, i32> {
        let c_path = CString::new(parent_path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_readdir;
        let free_buf = kernel.free_buf;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let mut out_buf: *mut u8 = std::ptr::null_mut();
            let mut out_len: usize = 0;
            let rc = unsafe { (fn_ptr)(kptr, c_path.as_ptr(), &mut out_buf, &mut out_len) };
            if rc != 0 {
                return Err(rc);
            }
            consume_string_out(free_buf, out_buf, out_len)
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_unlink`].
    pub async fn sys_unlink_async(kernel: &KernelHandle, path: &str) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_unlink;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let rc = unsafe { (fn_ptr)(kptr, c_path.as_ptr()) };
            if rc != 0 {
                return Err(rc);
            }
            Ok(())
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_mkdir`].
    pub async fn sys_mkdir_async(kernel: &KernelHandle, path: &str) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_mkdir;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let rc = unsafe { (fn_ptr)(kptr, c_path.as_ptr()) };
            if rc != 0 {
                return Err(rc);
            }
            Ok(())
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_rmdir`].
    pub async fn sys_rmdir_async(kernel: &KernelHandle, path: &str) -> Result<(), i32> {
        let c_path = CString::new(path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_rmdir;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let rc = unsafe { (fn_ptr)(kptr, c_path.as_ptr()) };
            if rc != 0 {
                return Err(rc);
            }
            Ok(())
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }

    /// Async wrapper around [`sys_rename`].
    pub async fn sys_rename_async(
        kernel: &KernelHandle,
        old_path: &str,
        new_path: &str,
    ) -> Result<(), i32> {
        let c_old = CString::new(old_path).map_err(|_| ERRNO_IO_RAW)?;
        let c_new = CString::new(new_path).map_err(|_| ERRNO_IO_RAW)?;
        let fn_ptr = kernel.sys_rename;
        let kptr = kernel.kernel_ptr as usize;
        tokio::task::spawn_blocking(move || {
            let kptr = kptr as *const std::ffi::c_void;
            let rc = unsafe { (fn_ptr)(kptr, c_old.as_ptr(), c_new.as_ptr()) };
            if rc != 0 {
                return Err(rc);
            }
            Ok(())
        })
        .await
        .unwrap_or(Err(ERRNO_IO_RAW))
    }
}

// ── kernel-side JSON shape parsers ──────────────────────────────────
//
// The kernel hand-rolls JSON in the C callbacks (no serde_json on the
// kernel side); we hand-roll the matching parsers here (no serde_json
// in the cdylib closure either).  Keeping them next to the
// wrappers — both sides consume the same SSOT shape.

/// DT_DIR entry_type byte — matches the `DT_DIR` constant in
/// `nexus-vfs/rust/kernel/src/abc/meta_store.rs`.
pub const DT_DIR: u64 = 1;
/// DT_MOUNT entry_type byte — matches the kernel-side constant.
/// Both DT_DIR and DT_MOUNT are surfaced as directories at the FUSE
/// / WinFsp layer; the FS protocol doesn't distinguish them.
pub const DT_MOUNT: u64 = 2;

/// Extract a `u64` from `json` by scanning for `key` and parsing
/// the digits that follow.  Returns `None` if the key isn't present
/// or the value isn't a non-negative integer.
pub fn json_u64(json: &str, key: &str) -> Option<u64> {
    let after = json.split_once(key)?.1.trim_start();
    let end = after
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(after.len());
    after[..end].parse().ok()
}

/// Extract `(size, entry_type)` from a `sys_stat` JSON payload.
/// Returns `None` only when the payload is unexpectedly malformed
/// (size key missing or non-numeric) — the kernel-side serializer
/// always emits both fields.
pub fn parse_stat(json: &str) -> Option<(u64, u64)> {
    let size = json_u64(json, "\"size\":")?;
    let entry_type = json_u64(json, "\"entry_type\":")?;
    Some((size, entry_type))
}

/// Parse the `sys_readdir` JSON array into `(name, entry_type)`
/// pairs.  The kernel-side serializer (see `kernel_cb_sys_readdir`
/// in `nexus-vfs/rust/kernel/src/kernel/plugins/mod.rs`) emits a
/// minimal JSON of shape `[{"name":<escaped>,"entry_type":<u8>}, ...]`,
/// escaping only `"` and `\` — extremely rare in path segments but
/// we honour them.
pub fn parse_readdir(json: &str) -> Vec<(String, u64)> {
    let mut out = Vec::new();
    let mut rest = json;
    while let Some((_, after_name)) = rest.split_once("\"name\":\"") {
        let mut name = String::new();
        let mut bytes = after_name.chars();
        loop {
            match bytes.next() {
                Some('\\') => match bytes.next() {
                    Some(c) => name.push(c),
                    None => return out,
                },
                Some('"') => break,
                Some(c) => name.push(c),
                None => return out,
            }
        }
        let after_close = bytes.as_str();
        let entry_type = match json_u64(after_close, "\"entry_type\":") {
            Some(t) => t,
            None => return out,
        };
        out.push((name, entry_type));
        rest = after_close;
    }
    out
}

// ── private helpers ─────────────────────────────────────────────────

/// Consume a HOST-allocated `(out_buf, out_len)` pair as a UTF-8
/// `String`, freeing the buffer via the host's `free_buf` even on
/// parse failure.  Both `sys_stat` and `sys_readdir` return JSON
/// payloads with this exact lifecycle.  `free_buf` MUST be the buffer's
/// allocating side (the host), never the plugin's `nexus_free` — the
/// two link different allocators (see the module docs).
fn consume_string_out(
    free_buf: NexusFreeFn,
    out_buf: *mut u8,
    out_len: usize,
) -> Result<String, i32> {
    unsafe {
        let slice = std::slice::from_raw_parts(out_buf, out_len);
        let s = std::str::from_utf8(slice)
            .map(|s| s.to_string())
            .map_err(|_| ERRNO_IO_RAW);
        free_buf(out_buf, out_len);
        s
    }
}

/// Consume a HOST-allocated `(out_buf, out_len)` pair as a byte vector,
/// freeing the buffer via the host's `free_buf`.  Used by `sys_read`
/// which doesn't constrain encoding.
fn consume_bytes_out(
    free_buf: NexusFreeFn,
    out_buf: *mut u8,
    out_len: usize,
) -> Result<Vec<u8>, i32> {
    unsafe {
        let slice = std::slice::from_raw_parts(out_buf, out_len);
        let v = slice.to_vec();
        free_buf(out_buf, out_len);
        Ok(v)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn json_u64_extracts_size() {
        assert_eq!(
            json_u64(r#"{"size":12345,"entry_type":0}"#, "\"size\":"),
            Some(12345)
        );
    }

    #[test]
    fn parse_stat_returns_both_fields() {
        let json = r#"{"path":"/foo","entry_type":1,"size":4096,"zone_id":"root"}"#;
        assert_eq!(parse_stat(json), Some((4096, 1)));
    }

    #[test]
    fn parse_readdir_mixed_kinds() {
        let json = r#"[{"name":"foo.txt","entry_type":0},{"name":"sub","entry_type":1}]"#;
        let entries = parse_readdir(json);
        assert_eq!(
            entries,
            vec![("foo.txt".to_string(), 0), ("sub".to_string(), 1)]
        );
    }

    #[test]
    fn parse_readdir_handles_escapes() {
        let json =
            r#"[{"name":"with \"quote\"","entry_type":0},{"name":"with\\back","entry_type":0}]"#;
        let entries = parse_readdir(json);
        assert_eq!(
            entries,
            vec![
                ("with \"quote\"".to_string(), 0),
                ("with\\back".to_string(), 0),
            ]
        );
    }

    #[test]
    fn parse_readdir_empty() {
        assert_eq!(parse_readdir("[]"), Vec::<(String, u64)>::new());
    }

    #[test]
    fn parse_stat_batch_output_mixed() {
        // Kernel side mirrors `[<size>,<entry_type>]` per slot, with
        // `null` for paths it couldn't stat.
        let json = "[[12,0],[4096,1],null,[65536,0]]";
        let parsed = parse_stat_batch_output(json);
        assert_eq!(
            parsed,
            vec![Some((12u64, 0u64)), Some((4096, 1)), None, Some((65536, 0)),]
        );
    }

    #[test]
    fn parse_stat_batch_output_empty() {
        assert_eq!(
            parse_stat_batch_output("[]"),
            Vec::<Option<(u64, u64)>>::new()
        );
    }
}
