//! Tier 2 CONVENIENCE — composes Tier 1. Tier 2 logic never goes in
//! `io.rs`.
//!
//! `KernelConvenience` is a supertrait of `KernelAbi` that provides
//! higher-level operations Python callers need (create-or-overwrite
//! write, xattr access, batch stat). Default implementations compose
//! `KernelAbi` methods; the `impl KernelConvenience for Kernel`
//! overrides with optimized direct paths where the composition
//! overhead matters.

use super::{
    Kernel, KernelError, OperationContext, StatResult, SysMkdirResult, SysReadResult,
    SysUnlinkResult, SysWriteResult,
};
use crate::abi::KernelAbi;
use crate::meta_store::{DT_EXTERNAL_STORAGE, DT_MOUNT};

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

    /// Tier 2 single-file unlink — composes `sys_unlink`.
    #[inline]
    fn unlink(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysUnlinkResult, KernelError> {
        self.sys_unlink(path, ctx, recursive)
    }

    /// Tier 2 single-file read — composes `sys_read`.
    #[inline]
    fn read(
        &self,
        path: &str,
        ctx: &OperationContext,
        timeout_ms: u64,
        offset: u64,
    ) -> Result<SysReadResult, KernelError> {
        self.sys_read(path, ctx, timeout_ms, offset)
    }

    /// Tier 2 write: create-or-overwrite.
    ///
    /// Composes `sys_write` + `setattr_update` for POSIX
    /// `open(O_CREAT|O_WRONLY) + write(2) + close(2)` semantics.
    /// When `sys_write` returns miss (file doesn't exist) and
    /// `offset == 0`, creates a DT_REG entry and retries.
    fn write(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
    ) -> Result<SysWriteResult, KernelError>;

    /// Tier 2 `mkdir` — create a directory.
    ///
    /// Conceptually `sys_setattr(entry_type=DT_DIR)` plus the
    /// `parents` / `exist_ok` directory-tree semantics. No default
    /// body — the composition needs kernel routing internals, so
    /// `Kernel` supplies the optimized inherent override (`io.rs`).
    fn mkdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        parents: bool,
        exist_ok: bool,
    ) -> Result<SysMkdirResult, KernelError>;

    /// `sys_stat(path).content_id` — single-field convenience.
    fn get_content_id(&self, path: &str, zone_id: &str) -> Option<String> {
        self.sys_stat(path, zone_id).and_then(|s| s.content_id)
    }

    /// `sys_stat(path).is_directory` — single-field convenience.
    fn is_directory(&self, path: &str, zone_id: &str) -> bool {
        self.sys_stat(path, zone_id)
            .map(|s| s.is_directory)
            .unwrap_or(false)
    }

    /// Top-level mount names: `sys_readdir("/")` filtered to DT_MOUNT / DT_EXTERNAL_STORAGE.
    fn get_top_level_mounts(&self, zone_id: &str) -> Vec<String> {
        let entries = self.sys_readdir("/", zone_id, true);
        let mut names: Vec<String> = entries
            .into_iter()
            .filter(|(_, et)| *et == DT_MOUNT || *et == DT_EXTERNAL_STORAGE)
            .filter_map(|(name, _)| {
                let top = name.trim_start_matches('/').split('/').next()?;
                if top.is_empty() {
                    None
                } else {
                    Some(top.to_string())
                }
            })
            .collect::<std::collections::BTreeSet<_>>()
            .into_iter()
            .collect();
        names.sort();
        names
    }

    /// Tier 2 batch write: composes `write()` per-item.
    ///
    /// Each item goes through `KernelConvenience::write()` which handles
    /// create-or-overwrite, hooks, and OBSERVE dispatch per-item
    /// automatically. Callers needing the raw Tier 1 batch path (sorted
    /// VFS locks, single redb txn) should use `sys_write` directly.
    fn write_batch(
        &self,
        items: &[(String, Vec<u8>)],
        ctx: &OperationContext,
    ) -> Vec<Result<SysWriteResult, KernelError>> {
        items
            .iter()
            .map(|(path, content)| self.write(path, ctx, content, 0))
            .collect()
    }

    /// Batch existence check: `stat_batch` → `Vec<bool>`.
    fn exists_batch(&self, paths: &[String], zone_id: &str) -> Vec<bool> {
        self.stat_batch(paths, zone_id)
            .into_iter()
            .map(|opt| opt.is_some())
            .collect()
    }
}

// ── `impl KernelConvenience for Kernel` — optimized overrides ────────

impl KernelConvenience for Kernel {
    fn access(&self, path: &str, zone_id: &str) -> bool {
        // Delegate to the inherent method on Kernel (io.rs).
        Kernel::access(self, path, zone_id)
    }

    fn mkdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        parents: bool,
        exist_ok: bool,
    ) -> Result<SysMkdirResult, KernelError> {
        // Delegate to the optimized inherent method on Kernel (io.rs).
        Kernel::mkdir(self, path, ctx, parents, exist_ok)
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
                        Some(entry) => Some(StatResult::from(entry)),
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

    fn write(
        &self,
        path: &str,
        ctx: &OperationContext,
        content: &[u8],
        offset: u64,
    ) -> Result<SysWriteResult, KernelError> {
        // Tier 2 create-or-overwrite. On miss + offset==0, creates DT_REG
        // via the same route object sys_write uses internally, then retries.
        // Uses sys_write_with_link_depth (kernel-internal) to guarantee the
        // create and retry share the identical metastore resolution path.
        let result = self.sys_write_with_link_depth(path, ctx, content, offset, 1)?;
        if !result.hit && offset == 0 {
            // Route-scoped create: resolve the same route sys_write uses,
            // build a bare DT_REG entry, put it through the route metastore.
            let route = self
                .vfs_router
                .route(path, &ctx.zone_id)
                .map_err(|_| KernelError::FileNotFound(path.to_string()))?;
            let now_ms = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0);
            let meta = self.build_metadata(
                path,
                &route.zone_id,
                crate::meta_store::DT_REG,
                0,
                None,
                0, // gen — first write, will be incremented by sys_write
                1,
                None,
                Some(now_ms),
                Some(now_ms),
            );
            self.with_metastore_route(&route, |ms| ms.put(path, meta))
                .ok_or_else(|| KernelError::IOError("no metastore wired".into()))
                .and_then(|r| {
                    r.map_err(|e| KernelError::IOError(format!("write create({path}): {e:?}")))
                })?;
            return self.sys_write_with_link_depth(path, ctx, content, offset, 1);
        }
        Ok(result)
    }
}
