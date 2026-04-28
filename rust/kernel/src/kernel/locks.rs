//! Advisory lock syscalls — `sys_lock`, `sys_unlock`, lock listing, federation install.
//!
//! Phase G of Phase 3 restructure plan extracted these methods from the
//! monolithic `kernel.rs` into a dedicated submodule.  The methods are
//! still members of [`Kernel`] via `impl Kernel { ... }` blocks — the
//! split is a file-organization change, not an API change.

use std::sync::Arc;

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

    /// Install a federation advisory-lock backend (R20.7 DI).
    ///
    /// Replaces the old ``upgrade_lock_manager``. First-wins per
    /// process: subsequent calls short-circuit BEFORE constructing a
    /// new ``DistributedLocks`` (which does a ``runtime.block_on``).
    /// Keeping the no-op fast matters for bootstrap paths that replay
    /// every mount — each replay would otherwise pay the block_on
    /// cost on the main thread.
    #[allow(dead_code)]
    pub fn install_federation_locks(
        &self,
        node: nexus_raft::prelude::ZoneConsensus<nexus_raft::prelude::FullStateMachine>,
        runtime: tokio::runtime::Handle,
    ) {
        if self.lock_manager.locks_installed() {
            return;
        }
        let kernel_state = self.lock_manager.advisory_state_arc();
        let (backend, shared_state) =
            nexus_raft::federation::DistributedLocks::new(node, runtime, kernel_state);
        let _installed = self
            .lock_manager
            .install_locks(Arc::new(backend), shared_state);
    }
}
