//! Tier 2 CONVENIENCE — composes Tier 1. Tier 2 logic never goes in
//! `io.rs`.
//!
//! `KernelConvenience` is a supertrait of `KernelAbi` that provides
//! higher-level operations Python callers need (create-or-overwrite
//! write, xattr access, batch stat). Default implementations compose
//! `KernelAbi` methods; the `impl KernelConvenience for Kernel`
//! overrides with optimized direct paths where the composition
//! overhead matters.

use std::any::Any;
use std::sync::Arc;

use super::{
    Kernel, KernelError, OperationContext, StatResult, SysMkdirResult, SysReadResult,
    SysRmdirResult, SysSetAttrResult, SysUnlinkResult, SysWriteResult,
};
use crate::abc::object_store::ObjectStore;
use crate::abi::KernelAbi;
use crate::meta_store::{MetaStore, DT_EXTERNAL_STORAGE, DT_MOUNT};
use crate::ROOT_ZONE_ID;

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

    /// Tier 2 mount — composes `sys_setattr(DT_MOUNT, …)`.
    ///
    /// Replaces the 21-positional-argument `sys_setattr` call shape
    /// (most positions are forced to `None`/`0` for DT_MOUNT) with a
    /// builder-style [`MountOptions`]. Every parameter that is
    /// definitionally inert for `DT_MOUNT` (capacity, FDs, mime,
    /// content_id, modified_at_ms, version, created_at_ms,
    /// link_target) is fixed at its no-op default here, leaving the
    /// caller to specify only mount-relevant fields. Behaviour is
    /// bit-identical to the equivalent `sys_setattr(DT_MOUNT, …)`
    /// call.
    fn mount(&self, path: &str, opts: MountOptions<'_>) -> Result<SysSetAttrResult, KernelError> {
        self.sys_setattr(
            path,
            DT_MOUNT as i32,
            opts.backend_name,
            opts.backend,
            opts.metastore,
            opts.raft_backend,
            opts.io_profile,
            opts.zone_id,
            opts.is_external,
            0,    // capacity (DT_PIPE / DT_STREAM only)
            None, // read_fd
            None, // write_fd
            None, // mime_type
            None, // modified_at_ms
            None, // content_id
            None, // size
            None, // version
            None, // created_at_ms
            None, // link_target (DT_LINK only)
            opts.source,
            opts.remote_metastore,
        )
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

    /// Tier 2 `rmdir` — remove a directory.
    ///
    /// Conceptually `sys_unlink(recursive=…)` narrowed to directories.
    /// No default body — `Kernel` supplies the optimized inherent
    /// override (`io.rs`), which the `sys_unlink` DT_DIR branch also
    /// calls directly.
    fn rmdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysRmdirResult, KernelError>;

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

// ── MountOptions ─────────────────────────────────────────────────────

/// Builder-style parameters for [`KernelConvenience::mount`].
///
/// Carries the DT_MOUNT-relevant subset of `sys_setattr` arguments
/// in a single value so callers do not thread 21 positional args
/// (most of which are `None` for DT_MOUNT) through every mount
/// site. Field semantics match `sys_setattr`'s DT_MOUNT branch
/// 1-for-1; this struct is purely a call-site ergonomics wrapper
/// and adds no kernel surface.
///
/// Construct via [`MountOptions::new`] and chain `with_*` setters
/// for the fields that differ from the kernel-default
/// owning-local-mount template.
///
/// ```ignore
/// use std::sync::Arc;
/// use kernel::kernel::convenience::{KernelConvenience, MountOptions};
/// kernel.mount(
///     "/scratch",
///     MountOptions::new("local").with_backend(backend),
/// )?;
/// ```
pub struct MountOptions<'a> {
    backend_name: &'a str,
    backend: Option<Arc<dyn ObjectStore>>,
    metastore: Option<Arc<dyn MetaStore>>,
    raft_backend: Option<Box<dyn Any + Send + Sync>>,
    io_profile: &'a str,
    zone_id: &'a str,
    is_external: bool,
    source: Option<&'a str>,
    remote_metastore: Option<Arc<dyn MetaStore>>,
}

impl<'a> MountOptions<'a> {
    /// Owning-local-mount template carrying `backend_name` (driver
    /// label, e.g. `"local"`, `"memory"`, `"s3"`).
    ///
    /// Defaults: no backend handle, no metastore handle, no raft
    /// backend, `"memory"` io_profile, root zone, not external,
    /// no federation source, no remote metastore. Override the
    /// fields that differ via the chainable `with_*` setters.
    pub fn new(backend_name: &'a str) -> Self {
        Self {
            backend_name,
            backend: None,
            metastore: None,
            raft_backend: None,
            io_profile: "memory",
            zone_id: ROOT_ZONE_ID,
            is_external: false,
            source: None,
            remote_metastore: None,
        }
    }

    /// Attach an owning `ObjectStore` (required for owning mounts;
    /// federation join-mode mounts leave this unset).
    pub fn with_backend(mut self, backend: Arc<dyn ObjectStore>) -> Self {
        self.backend = Some(backend);
        self
    }

    /// Attach a per-mount metastore. Default = the kernel's global
    /// metastore handles this mount.
    pub fn with_metastore(mut self, metastore: Arc<dyn MetaStore>) -> Self {
        self.metastore = Some(metastore);
        self
    }

    /// Attach a raft backend handle (federation use only). Erased
    /// to `Box<dyn Any>` to keep the kernel crate free of raft
    /// types.
    pub fn with_raft_backend(mut self, raft_backend: Box<dyn Any + Send + Sync>) -> Self {
        self.raft_backend = Some(raft_backend);
        self
    }

    /// Override the io_profile label. Default = `"memory"`.
    pub fn with_io_profile(mut self, io_profile: &'a str) -> Self {
        self.io_profile = io_profile;
        self
    }

    /// Override the zone id. Default = `ROOT_ZONE_ID` (`"root"`).
    pub fn with_zone(mut self, zone_id: &'a str) -> Self {
        self.zone_id = zone_id;
        self
    }

    /// Mark this mount as external storage (DT_EXTERNAL_STORAGE
    /// semantics).
    pub fn external(mut self) -> Self {
        self.is_external = true;
        self
    }

    /// Mark this as a federation join-mode mount with the given
    /// leader address (`host:port`). The mount entry then points
    /// at a remote zone rather than creating one locally.
    pub fn with_source(mut self, source: &'a str) -> Self {
        self.source = Some(source);
        self
    }

    /// Attach a remote metastore (federation: produced by the
    /// object-store provider when the backend is remote). Installed
    /// on the VFS route entry so remote reads resolve through the
    /// correct metastore.
    pub fn with_remote_metastore(mut self, remote_metastore: Arc<dyn MetaStore>) -> Self {
        self.remote_metastore = Some(remote_metastore);
        self
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

    fn rmdir(
        &self,
        path: &str,
        ctx: &OperationContext,
        recursive: bool,
    ) -> Result<SysRmdirResult, KernelError> {
        // Delegate to the optimized inherent method on Kernel (io.rs).
        Kernel::rmdir(self, path, ctx, recursive)
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
                .ok_or_else(|| KernelError::FileNotFound(path.to_string()))?;
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
