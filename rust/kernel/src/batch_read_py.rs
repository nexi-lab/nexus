//! PyO3 helper for batch-read error classification (Issue #4058).

use crate::kernel::KernelError;

pub fn batch_err_kind_msg(e: &KernelError) -> (String, String) {
    match e {
        KernelError::FileNotFound(p) => ("not_found".into(), p.clone()),
        KernelError::PermissionDenied(m) => ("permission_denied".into(), m.clone()),
        KernelError::InvalidPath(m) => ("invalid_path".into(), m.clone()),
        other => ("io_error".into(), format!("{:?}", other)),
    }
}
