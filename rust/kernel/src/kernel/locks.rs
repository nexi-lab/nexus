//! Advisory lock syscalls — `sys_lock`, `sys_unlock`, lock listing.
//!
//! Phase G of Phase 3 restructure plan extracted these methods from the
//! monolithic `kernel.rs` into a dedicated submodule.  The methods are
//! still members of [`Kernel`] via `impl Kernel { ... }` blocks — the
//! split is a file-organization change, not an API change.
//!
//! Phase H of the same plan moved the federation distributed-lock
//! install (`install_federation_locks` — formerly here) into the raft
//! crate's `RaftFederationProvider` impl, where it can name
//! `nexus_raft::federation::DistributedLocks` directly.  Kernel-side
//! callers reach the install through the `FederationProvider` trait
//! dispatch.

use super::{Kernel, KernelError};

impl Kernel {
    // ── Advisory lock primitive (§4.4) ──────────────────────────

    /// Acquire or extend an advisory lock.
    ///
    /// `lock_id` empty → try-acquire (returns `Some(new_uuid)` or
    /// `None` on conflict). `lock_id` non-empty → extend TTL
    /// (returns `Some(lock_id)` or `None` if holder not found).
    #[allow(clippy::too_many_arguments)]
    pub fn sys_lock(
        &self,
        path: &str,
        lock_id: &str,
        mode: crate::lock_manager::KernelLockMode,
        max_holders: u32,
        ttl_secs: u64,
        holder_info: &str,
    ) -> Result<Option<String>, KernelError> {
        if lock_id.is_empty() {
            let generated_id = uuid::Uuid::new_v4().to_string();
            let acquired = self
                .lock_manager
                .acquire_lock(
                    path,
                    &generated_id,
                    mode,
                    max_holders,
                    ttl_secs,
                    holder_info,
                )
                .map_err(|e| KernelError::IOError(format!("sys_lock({path}): {e}")))?;
            Ok(if acquired { Some(generated_id) } else { None })
        } else {
            let extended = self
                .lock_manager
                .extend_lock(path, lock_id, ttl_secs)
                .map_err(|e| KernelError::IOError(format!("sys_lock({path}): {e}")))?;
            Ok(if extended {
                Some(lock_id.to_string())
            } else {
                None
            })
        }
    }

    /// Release a specific holder, or force-release all holders.
    pub fn sys_unlock(&self, path: &str, lock_id: &str, force: bool) -> Result<bool, KernelError> {
        if force {
            self.lock_manager
                .force_release_lock(path)
                .map_err(|e| KernelError::IOError(format!("sys_unlock({path}): {e}")))
        } else {
            self.lock_manager
                .release_lock(path, lock_id)
                .map_err(|e| KernelError::IOError(format!("sys_unlock({path}): {e}")))
        }
    }

    /// Enumerate locks under `prefix`, capped at `limit`.
    pub fn metastore_list_locks(
        &self,
        prefix: &str,
        limit: usize,
    ) -> Result<Vec<crate::lock_manager::KernelLockInfo>, KernelError> {
        self.lock_manager
            .list_locks(prefix, limit)
            .map_err(|e| KernelError::IOError(format!("metastore_list_locks({prefix}): {e}")))
    }
}
