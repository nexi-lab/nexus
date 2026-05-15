//! PyO3 helper for batch-read error classification (Issue #4058).

use crate::kernel::KernelError;

pub fn batch_err_kind_msg(e: &KernelError) -> (String, String) {
    match e {
        KernelError::FileNotFound(p) => ("not_found".into(), p.clone()),
        KernelError::PermissionDenied(m) => ("permission_denied".into(), m.clone()),
        KernelError::InvalidPath(m) => ("invalid_path".into(), m.clone()),
        // Distinguish kernel-internal "this path's entry type doesn't
        // belong in the batch fast path" rejections from generic I/O
        // errors. The Python wrapper recognizes "unsupported" and falls
        // through to the single-file read path (resolves DT_PIPE,
        // DT_STREAM, external connectors, virtual resolvers, etc.).
        KernelError::IOError(m) if m.starts_with("batch read does not support") => {
            ("unsupported".into(), m.clone())
        }
        other => ("io_error".into(), format!("{:?}", other)),
    }
}
