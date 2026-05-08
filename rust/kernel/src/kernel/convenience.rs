//! Tier 2 convenience methods — composed from Tier 1 syscalls.
//!
//! `KernelConvenience` is a supertrait of `KernelAbi` that provides
//! higher-level operations Python callers need (xattr access, batch
//! stat, full stat with xattrs). Default implementations compose
//! `KernelAbi` methods; the `impl KernelConvenience for Kernel`
//! overrides with optimized direct-metastore paths where the
//! composition overhead matters (batch stat uses a single redb read
//! txn instead of N sys_stat calls).

use super::{Kernel, KernelError, StatResult};
use crate::abi::KernelAbi;
use crate::meta_store::{DT_DIR, DT_MOUNT};

// ── KernelConvenience trait ──────────────────────────────────────────

/// Tier 2 convenience surface — composed from Tier 1 `KernelAbi`
/// syscalls, with optimized overrides on the concrete `Kernel`.
pub trait KernelConvenience: KernelAbi {
    /// Fast existence check: validate + route + metastore.exists.
    fn access(&self, path: &str, zone_id: &str) -> bool;

    /// Batch stat: returns `Vec<Option<StatResult>>` aligned with input.
    /// Default: N × sys_stat. Override: single redb read txn.
    fn stat_batch(&self, paths: &[String], zone_id: &str) -> Vec<Option<StatResult>> {
        paths.iter().map(|p| self.sys_stat(p, zone_id)).collect()
    }

    /// Set an extended attribute on `path`.
    fn set_xattr(
        &self,
        path: &str,
        key: &str,
        value: String,
        zone_id: &str,
    ) -> Result<(), KernelError>;

    /// Get an extended attribute from `path`.
    fn get_xattr(
        &self,
        path: &str,
        key: &str,
        zone_id: &str,
    ) -> Result<Option<String>, KernelError>;

    /// Bulk get a single xattr key across multiple paths.
    /// Returns Vec of (path, Option<value>) aligned with input.
    fn get_xattr_bulk(
        &self,
        paths: &[String],
        key: &str,
        zone_id: &str,
    ) -> Result<Vec<(String, Option<String>)>, KernelError>;
}

// ── `impl KernelConvenience for Kernel` — optimized overrides ────────

impl KernelConvenience for Kernel {
    fn access(&self, path: &str, zone_id: &str) -> bool {
        // Delegate to the inherent method on Kernel (io.rs).
        Kernel::access(self, path, zone_id)
    }

    fn stat_batch(&self, paths: &[String], zone_id: &str) -> Vec<Option<StatResult>> {
        // Optimized: use metastore.get_batch in a single redb read txn,
        // then convert to StatResult. Falls back to per-path sys_stat
        // for paths that need special handling (procfs, implicit dirs).
        let mount_point = if let Some(first) = paths.first() {
            self.resolve_mount_point(first, zone_id)
        } else {
            return Vec::new();
        };

        let batch_result = self.with_metastore(&mount_point, |ms| ms.get_batch(paths));
        match batch_result {
            Some(Ok(metas)) => metas
                .into_iter()
                .enumerate()
                .map(|(i, opt)| {
                    match opt {
                        Some(entry) => {
                            let is_dir = entry.entry_type == DT_DIR || entry.entry_type == DT_MOUNT;
                            let mime = entry
                                .mime_type
                                .as_deref()
                                .unwrap_or(if is_dir {
                                    "inode/directory"
                                } else {
                                    "application/octet-stream"
                                })
                                .to_string();
                            Some(StatResult {
                                path: entry.path.clone(),
                                size: if is_dir && entry.size == 0 {
                                    4096
                                } else {
                                    entry.size
                                },
                                content_id: entry.content_id.clone(),
                                mime_type: mime,
                                is_directory: is_dir,
                                entry_type: entry.entry_type,
                                mode: if is_dir { 0o755 } else { 0o644 },
                                version: entry.version,
                                gen: entry.gen,
                                zone_id: entry.zone_id.clone(),
                                created_at_ms: entry.created_at_ms,
                                modified_at_ms: entry.modified_at_ms,
                                last_writer_address: entry.last_writer_address.clone(),
                                lock: None, // batch stat skips lock info for perf
                                link_target: entry.link_target.clone(),
                            })
                        }
                        None => {
                            // Fallback to sys_stat for implicit dirs, procfs, etc.
                            self.sys_stat(&paths[i], zone_id)
                        }
                    }
                })
                .collect(),
            // Fallback: different mounts or error — per-path sys_stat.
            _ => paths.iter().map(|p| self.sys_stat(p, zone_id)).collect(),
        }
    }

    fn set_xattr(
        &self,
        path: &str,
        key: &str,
        value: String,
        _zone_id: &str,
    ) -> Result<(), KernelError> {
        // Direct metastore access — bypasses hooks (xattr is metadata, not content).
        self.metastore_set_file_metadata(path, key, value)
    }

    fn get_xattr(
        &self,
        path: &str,
        key: &str,
        _zone_id: &str,
    ) -> Result<Option<String>, KernelError> {
        self.metastore_get_file_metadata(path, key)
    }

    fn get_xattr_bulk(
        &self,
        paths: &[String],
        key: &str,
        _zone_id: &str,
    ) -> Result<Vec<(String, Option<String>)>, KernelError> {
        self.metastore_get_file_metadata_bulk(paths, key)
    }
}
