//! `MetaStore` ABC — §3 metadata pillar.
//!
//! Rust mirror of Python `MetastoreABC` (one of the Three Storage
//! Pillars in `KERNEL-ARCHITECTURE.md` §3). Provides ordered key-value
//! storage for file metadata (inodes, config, topology).
//!
//! The `abc/` directory holds the strict §3 invariant set (3 trait
//! files, period). Concrete impls (`MemoryMetaStore` reference impl +
//! `LocalMetaStore` redb impl) live in `crate::core::meta_store`. The
//! remote / raft impls live in their respective parallel crates
//! (`raft::meta_store`).
//!
//! Pure Rust ABI — no PyO3 dependency. PyO3 adapters live in
//! `generated_kernel_abi_pyo3.rs` (auto-generated).
//!
//! Naming note: the Rust trait is `MetaStore` for visual symmetry with
//! `ObjectStore` / `CacheStore`. The Python ABC stays `MetastoreABC`
//! (sunset path); the cross-language asymmetry is anchored at exactly
//! one PyO3 boundary in `raft/src/pyo3_bindings.rs`.

/// Metadata record for a single file/directory.
///
/// Mirrors the Python `FileMetadata` fields needed by the Rust kernel.
///
/// Schema notes:
/// - `path` is the authoritative file identifier. Read-side backend dispatch
///   uses it (minus the local mount prefix) — there is no separate
///   `physical_path` because for path-addressed backends it is just
///   `path - mount_prefix`, and for content-addressed backends `content_id` is
///   the key.
/// - `last_writer_address` records `host:port` of the node that performed
///   the most recent write (overwritten on every successful write). Pure
///   descriptive metadata — the kernel does not interpret it. Higher
///   layers (e.g. federation) compare it against the local node address
///   to route content fetches. There is no per-record `backend_name`:
///   each node picks its backend from its own mount table.
#[derive(Clone, Debug, Default)]
pub struct FileMetadata {
    pub path: String,
    pub size: u64,
    pub content_id: Option<String>,
    pub version: u32,
    pub entry_type: u8,
    pub zone_id: Option<String>,
    pub mime_type: Option<String>,
    /// Creation timestamp (Unix epoch milliseconds). Populated by
    /// ``kernel::sys_write`` on first write; subsequent overwrites preserve
    /// it via the dcache snapshot.
    pub created_at_ms: Option<i64>,
    /// Last modification timestamp (Unix epoch milliseconds). Updated on
    /// every write.
    pub modified_at_ms: Option<i64>,
    /// `host:port` of the node that performed the most recent write
    /// (overwritten on every successful write). Set by the kernel from
    /// its self-published address. Higher layers (federation) interpret
    /// it; the kernel only stores and forwards. `None` on single-node
    /// deployments without a published address.
    pub last_writer_address: Option<String>,
    /// For `entry_type == DT_MOUNT (2)`: the zone this mount points
    /// to.  Federation's `mount_apply_cb` reads this on every replicated
    /// SetMetadata to wire cross-zone routing on followers — it must
    /// round-trip through the metastore proto, so we carry it on the
    /// kernel struct rather than reconstructing it from a sibling
    /// channel.  `None` for non-DT_MOUNT entries.
    pub target_zone_id: Option<String>,
    /// For `entry_type == DT_LINK (6)`: absolute or workspace-relative
    /// VFS path the link resolves to.  `Some` only when entry_type is
    /// DT_LINK.  One-hop resolution at `route()` time with self-loop
    /// rejection at `sys_setattr` write time.  See
    /// `KERNEL-ARCHITECTURE.md` §4.5.
    pub link_target: Option<String>,
}

/// Error type for `MetaStore` operations.
#[derive(Debug)]
pub enum MetaStoreError {
    /// Key not found.
    NotFound(String),
    /// Underlying I/O or storage error.
    IOError(String),
}

/// Result of a `put_if_version` optimistic-concurrency check.
///
/// Naming note: "CAS" in the kernel already means **Content-Addressed
/// Storage** (see `cas_engine.rs`) — the blob pillar. This struct is
/// the unrelated *compare-and-swap* primitive used by the metastore's
/// version guard on `put`, so it is spelled out in full to avoid
/// collision with the CAS blob namespace.
#[derive(Debug, Clone, Copy)]
pub struct PutIfVersionResult {
    /// True if the write was applied.
    pub success: bool,
    /// Version currently in the store after this call (new version on
    /// success, existing version on conflict).
    pub current_version: u32,
}

/// `(path, optional value)` pairs used by bulk auxiliary-metadata reads
/// and bulk content-id lookups. Values are UTF-8 strings — every real
/// caller stores text (`parsed_text`, `parser_name`, JSON-encoded
/// blobs), so the kernel boundary avoids a `PyBytes` GIL crossing.
pub type PathValueStr = (String, Option<String>);
pub type PathEtag = (String, Option<String>);

/// One page of a paginated list scan.
#[derive(Debug, Default, Clone)]
pub struct PaginatedList {
    pub items: Vec<FileMetadata>,
    pub next_cursor: Option<String>,
    pub has_more: bool,
    pub total_count: usize,
}

/// MetaStore pillar — kernel metadata contract.
///
/// Rust equivalent of Python `MetastoreABC`.
/// Local impls (redb) implement directly; remote impls go through
/// existing gRPC network boundaries.
///
/// 5 abstract methods matching the Python ABC:
///   - get, put, delete, list, exists
///
/// **Key contract**: callers always pass full global paths —
/// including the mount-point prefix. Impls that store zone-relative
/// internally (``ZoneMetaStore``) translate at their boundary so
/// federation-layer concerns never leak up. Returned ``FileMetadata.path``
/// values are likewise full paths.
pub trait MetaStore: Send + Sync {
    /// Get metadata for a path. Returns None if not found.
    fn get(&self, path: &str) -> Result<Option<FileMetadata>, MetaStoreError>;

    /// Put metadata at a path (insert or update).
    fn put(&self, path: &str, metadata: FileMetadata) -> Result<(), MetaStoreError>;

    /// Delete metadata at a path. Returns true if it existed.
    fn delete(&self, path: &str) -> Result<bool, MetaStoreError>;

    /// List all metadata entries under a prefix.
    fn list(&self, prefix: &str) -> Result<Vec<FileMetadata>, MetaStoreError>;

    /// Check if a path exists in the metastore.
    fn exists(&self, path: &str) -> Result<bool, MetaStoreError>;

    /// Batch put: store multiple metadata records.
    /// Default impl loops single puts. Override for single-transaction batch.
    fn put_batch(&self, items: &[(String, FileMetadata)]) -> Result<(), MetaStoreError> {
        for (path, meta) in items {
            self.put(path, meta.clone())?;
        }
        Ok(())
    }

    /// Batch get: retrieve metadata for multiple paths.
    /// Default impl loops single gets.
    fn get_batch(&self, paths: &[String]) -> Result<Vec<Option<FileMetadata>>, MetaStoreError> {
        let mut results = Vec::with_capacity(paths.len());
        for path in paths {
            results.push(self.get(path)?);
        }
        Ok(results)
    }

    /// Batch delete: remove metadata for multiple paths.
    /// Returns number of entries that existed and were deleted.
    /// Default impl loops single deletes. Override for single-transaction batch.
    fn delete_batch(&self, paths: &[String]) -> Result<usize, MetaStoreError> {
        let mut count = 0;
        for path in paths {
            if self.delete(path)? {
                count += 1;
            }
        }
        Ok(count)
    }

    /// Compare-and-swap put: write only if current stored version equals
    /// `expected_version`. Returns a `PutIfVersionResult` whose `current_version`
    /// field reflects the state after this call (caller can use the
    /// mismatch case to rebuild a retry).
    ///
    /// Default impl is racy (get → compare → put). Redb overrides with a
    /// single write txn; ZoneMetaStore overrides with a raft propose.
    fn put_if_version(
        &self,
        metadata: FileMetadata,
        expected_version: u32,
    ) -> Result<PutIfVersionResult, MetaStoreError> {
        let path = metadata.path.clone();
        let current = self.get(&path)?;
        let current_ver = current.as_ref().map(|m| m.version).unwrap_or(0);
        if current_ver != expected_version {
            return Ok(PutIfVersionResult {
                success: false,
                current_version: current_ver,
            });
        }
        let new_ver = metadata.version;
        self.put(&path, metadata)?;
        Ok(PutIfVersionResult {
            success: true,
            current_version: new_ver,
        })
    }

    /// Rename a path (and optionally all children, if the path is a
    /// directory with entries under `old_path + "/"`).
    ///
    /// Default impl: rewrites `old_path` entry and every entry under
    /// `old_path + "/"` prefix via get → put(new_key) → delete(old_key).
    /// Not atomic under concurrent writers — callers that need
    /// atomicity override (redb uses a single write txn).
    ///
    /// `is_pas`: pass `true` for path-addressed backends (local://, GDrive, etc.)
    /// so implementations update `content_id` to the new path; pass `false` for
    /// content-addressed backends (CAS) where `content_id` is a hash and must
    /// not be rewritten.
    fn rename_path(
        &self,
        old_path: &str,
        new_path: &str,
        is_pas: bool,
    ) -> Result<(), MetaStoreError> {
        if old_path == new_path {
            return Ok(());
        }
        if let Some(mut meta) = self.get(old_path)? {
            meta.path = new_path.to_string();
            if is_pas {
                pas_update_content_id(&mut meta, old_path, new_path);
            }
            self.put(new_path, meta)?;
            self.delete(old_path)?;
        }
        let old_prefix = format!("{}/", old_path.trim_end_matches('/'));
        let new_prefix = format!("{}/", new_path.trim_end_matches('/'));
        let children = self.list(&old_prefix)?;
        for mut child in children {
            let suffix = match child.path.strip_prefix(&old_prefix) {
                Some(s) => s.to_string(),
                None => continue,
            };
            let old_child = child.path.clone();
            let new_child = format!("{}{}", new_prefix, suffix);
            child.path = new_child.clone();
            if is_pas {
                pas_update_content_id(&mut child, &old_child, &new_child);
            }
            self.put(&new_child, child)?;
            self.delete(&old_child)?;
        }
        Ok(())
    }

    /// Store an auxiliary key/value blob attached to a path (e.g.
    /// `parsed_text`, tags, observer state). Separate namespace from the
    /// `FileMetadata` struct fields.
    ///
    /// Default impl returns an error — each concrete impl must provide
    /// its own storage (DashMap sidecar, second redb table, raft
    /// command).
    fn set_file_metadata(
        &self,
        path: &str,
        key: &str,
        value: String,
    ) -> Result<(), MetaStoreError> {
        let _ = (path, key, value);
        Err(MetaStoreError::IOError(
            "set_file_metadata not implemented for this metastore".into(),
        ))
    }

    /// Read an auxiliary key/value blob. Default impl returns `Ok(None)`.
    fn get_file_metadata(&self, path: &str, key: &str) -> Result<Option<String>, MetaStoreError> {
        let _ = (path, key);
        Ok(None)
    }

    /// Bulk read a single auxiliary key across multiple paths. Default
    /// impl loops `get_file_metadata`.
    fn get_file_metadata_bulk(
        &self,
        paths: &[String],
        key: &str,
    ) -> Result<Vec<PathValueStr>, MetaStoreError> {
        let mut out = Vec::with_capacity(paths.len());
        for p in paths {
            out.push((p.clone(), self.get_file_metadata(p, key)?));
        }
        Ok(out)
    }

    /// Return true if `path` has any children under `path + "/"`.
    fn is_implicit_directory(&self, path: &str) -> Result<bool, MetaStoreError> {
        let prefix = format!("{}/", path.trim_end_matches('/'));
        let children = self.list(&prefix)?;
        Ok(!children.is_empty())
    }

    /// Paginated list. Default impl materializes `list(prefix)` and
    /// slices. Override for backends where streaming matters.
    fn list_paginated(
        &self,
        prefix: &str,
        recursive: bool,
        limit: usize,
        cursor: Option<&str>,
    ) -> Result<PaginatedList, MetaStoreError> {
        let mut all = self.list(prefix)?;
        if !recursive {
            let depth = prefix.trim_end_matches('/').matches('/').count() + 1;
            all.retain(|m| m.path.trim_end_matches('/').matches('/').count() == depth);
        }
        let start: usize = cursor.and_then(|c| c.parse().ok()).unwrap_or(0);
        let end = (start + limit).min(all.len());
        let items = all[start..end].to_vec();
        let has_more = end < all.len();
        let next_cursor = if has_more {
            Some(end.to_string())
        } else {
            None
        };
        Ok(PaginatedList {
            items,
            next_cursor,
            has_more,
            total_count: all.len(),
        })
    }

    /// Bulk fetch content IDs (etags) for many paths. Default impl
    /// loops `get` and returns the content_id from each record.
    fn batch_get_content_ids(&self, paths: &[String]) -> Result<Vec<PathEtag>, MetaStoreError> {
        let mut out = Vec::with_capacity(paths.len());
        for p in paths {
            let content_id = self.get(p)?.and_then(|m| m.content_id);
            out.push((p.clone(), content_id));
        }
        Ok(out)
    }

    /// Opaque identity for "stores backed by the SAME underlying state".
    ///
    /// Two ``Arc<dyn MetaStore>`` can correspond to different VFS mount
    /// points yet share the same physical storage — the canonical case
    /// is a single federation zone surfaced under ``/corp`` AND
    /// ``/family/work`` (crosslink). Each crosslink gets its own
    /// ``ZoneMetaStore`` (different ``mount_point``), so ``Arc::ptr_eq``
    /// does not suffice to find every mount that shares the same zone.
    ///
    /// Return ``Some(usize)`` with a stable integer key for all
    /// metastores that share physical storage (``Arc::as_ptr`` of the
    /// shared handle works well — integer comparison, no lifetime
    /// entanglement). Return ``None`` when the metastore is standalone
    /// (``LocalMetaStore``) — the default.
    ///
    /// Used by ``VFSRouter::mount_points_for_coherence_key`` to fan
    /// out apply-side dcache invalidation across crosslinks.
    fn coherence_key(&self) -> Option<usize> {
        None
    }

    /// Append a raw byte entry at `key` to the metastore's
    /// stream-entries side table.  Used by `WalStreamCore` /
    /// `WalPipeCore` (the durable DT_STREAM / DT_PIPE backends) to
    /// persist an entry through whatever replication the metastore
    /// happens to provide — ``LocalMetaStore`` has no replication and
    /// returns ``NotSupported`` (callers fall back to the in-memory
    /// stream/pipe backends), ``ZoneMetaStore`` proposes
    /// ``Command::AppendStreamEntry`` so peers see the entry via
    /// raft commit.
    ///
    /// The metastore impl owns the key namespace — kernel-side
    /// callers prefix with ``__wal_stream__/<id>`` or
    /// ``__wal_pipe__/<id>`` so stream and pipe entries never collide
    /// in the shared side table.
    fn append_stream_entry(&self, key: &str, data: &[u8]) -> Result<(), MetaStoreError> {
        let _ = (key, data);
        Err(MetaStoreError::IOError(
            "append_stream_entry: not supported by this metastore (use a distributed impl, e.g. ZoneMetaStore)".to_string(),
        ))
    }

    /// Read a raw byte entry at `key` from the metastore's
    /// stream-entries side table.  Counterpart to
    /// [`Self::append_stream_entry`].  ``Ok(None)`` means the entry
    /// has not been written yet (cursor ahead of writer); ``Err``
    /// surfaces I/O / state-machine failures.
    fn get_stream_entry(&self, key: &str) -> Result<Option<Vec<u8>>, MetaStoreError> {
        let _ = key;
        Err(MetaStoreError::IOError(
            "get_stream_entry: not supported by this metastore (use a distributed impl, e.g. ZoneMetaStore)".to_string(),
        ))
    }
}

/// Update `content_id` in a PAS `FileMetadata` entry after a rename.
///
/// PAS (path-addressed storage) backends store `content_id` equal to the
/// backend-relative path (e.g. `"file.txt"` or `"sub/file.txt"`).  After a
/// rename, `content_id` must be updated to the new path or `sys_read` will
/// attempt to read from the old (now non-existent) location.
///
/// Only call this when `route.is_cas == false` — for CAS backends `content_id`
/// is a hash and must not be overwritten with a path string.
pub fn pas_update_content_id(meta: &mut FileMetadata, old_vfs: &str, new_vfs: &str) {
    if let Some(cid) = meta.content_id.as_deref() {
        let pas_suffix = format!("/{cid}");
        if old_vfs.ends_with(pas_suffix.as_str()) || old_vfs == cid {
            let prefix_len = old_vfs.len() - cid.len();
            let mount_prefix = &old_vfs[..prefix_len];
            if new_vfs.starts_with(mount_prefix) {
                meta.content_id = Some(new_vfs[prefix_len..].to_string());
            }
        }
    }
}
