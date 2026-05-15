//! Path-addressed storage trait — `PathAddressingEngine`.
//!
//! Rust mirror of Python `nexus.backends.base.path_addressing_engine.PathAddressingEngine`.
//! Layered on top of [`kernel::abc::object_store::ObjectStore`] — every
//! `PathAddressingEngine` is also an `ObjectStore`, but additionally
//! commits to *path-as-content-id* semantics: blobs live at their
//! actual paths, no CAS hashing, no deduplication.
//!
//! The trait surface adds the methods that path-addressed callers need
//! beyond the basic [`ObjectStore`] CRUD:
//!
//!   - **Streaming reads / writes** — large blobs that don't fit in
//!     memory; iterator-based to stay sync.
//!   - **Path-keyed metadata** — `get_size_by_path` /
//!     `get_version_by_path` use the backend path directly, where the
//!     plain ObjectStore methods are content-id keyed.
//!   - **Existence + directory checks** — `content_exists` /
//!     `is_directory` for kernel-side mount + dispatch.
//!   - **Batch operations** — fan-out reads / versions / writes /
//!     deletes; drivers that have a native batch API override, others
//!     fall back to sequential.
//!
//! `Send + Sync` mirrors the `ObjectStore` super-trait — a path
//! backend shared across syscall threads must be both.

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};

/// Iterator handle returned by [`PathAddressingEngine::stream_content`]
/// and [`PathAddressingEngine::stream_file`].
///
/// Each item is a content chunk; iteration ends when the source is
/// exhausted or the connection closes.
pub type ContentStream = Box<dyn Iterator<Item = Result<Vec<u8>, StorageError>> + Send>;

/// Result row for [`PathAddressingEngine::batch_read_content`] — a
/// `(content_id, content_or_none)` tuple where `None` means the
/// per-item read failed.
pub type BatchReadRow = (String, Option<Vec<u8>>);

/// Result row for [`PathAddressingEngine::batch_get_versions`] — a
/// `(backend_path, version_or_none)` tuple where `None` means the
/// transport is non-versioned or the path is unknown.
pub type BatchVersionRow = (String, Option<String>);

/// Result row for [`PathAddressingEngine::batch_write_content`] — a
/// `(content_id, write_result_or_none)` tuple where `None` means the
/// per-item write failed.
pub type BatchWriteRow = (String, Option<WriteResult>);

/// Result row for [`PathAddressingEngine::batch_delete_content`] — a
/// `(content_id, ok)` tuple recording per-item success.
pub type BatchDeleteRow = (String, bool);

/// Path-addressed storage backend.
///
/// All methods take a `backend_path` (relative to the backend root)
/// rather than a CAS hash; `content_id` parameters carry version IDs
/// for backends with versioning enabled, otherwise the path itself.
pub trait PathAddressingEngine: ObjectStore {
    // ─── Streaming I/O ───────────────────────────────────────────────────

    /// Stream blob content as an iterator of byte chunks.  Used for
    /// large blobs that do not fit in memory (cross-backend copy,
    /// federated read pipelines).
    fn stream_content(
        &self,
        content_id: &str,
        backend_path: &str,
        chunk_size: usize,
    ) -> Result<ContentStream, StorageError>;

    /// Stream a file by backend path.  Same shape as
    /// [`stream_content`](Self::stream_content) but does not require a
    /// content-id (path-only addressing).
    fn stream_file(&self, path: &str, chunk_size: usize) -> Result<ContentStream, StorageError>;

    /// Write a file from an iterator of byte chunks.  Drivers that
    /// support native multipart / resumable uploads (S3, GCS) use them
    /// here; local drivers concatenate.
    ///
    /// Returns the final version id when the underlying transport
    /// reports one (S3 versioning, GCS generation), otherwise `None`.
    fn write_file_chunked(
        &self,
        path: &str,
        chunks: Box<dyn Iterator<Item = Result<Vec<u8>, StorageError>> + Send>,
        content_type: &str,
    ) -> Result<Option<String>, StorageError>;

    // ─── Path-keyed metadata ─────────────────────────────────────────────

    /// Return blob size for a backend path.  Distinct from
    /// [`ObjectStore::get_content_size`] which is keyed by content-id.
    fn get_size_by_path(&self, backend_path: &str) -> Result<u64, StorageError>;

    /// Return blob version / generation for a backend path, or `None`
    /// when the underlying transport is not versioned.
    fn get_version_by_path(&self, backend_path: &str) -> Result<Option<String>, StorageError>;

    // ─── Existence + directory checks ────────────────────────────────────

    /// Check whether a blob exists at the given backend path.
    fn content_exists(&self, content_id: &str, backend_path: &str) -> Result<bool, StorageError>;

    /// Check whether the path resolves to a directory.
    fn is_directory(&self, path: &str) -> Result<bool, StorageError>;

    // ─── Batch operations ────────────────────────────────────────────────
    //
    // Default impls fan out via sequential calls so a driver only
    // needs to implement the singular methods; cloud drivers override
    // with native batch APIs when latency matters.

    /// Read multiple blobs by `(content_id, backend_path)` pairs.  The
    /// returned map keeps `None` for entries that failed individually
    /// — the call as a whole succeeds.
    fn batch_read_content(
        &self,
        items: &[(String, String)],
    ) -> Result<Vec<BatchReadRow>, StorageError> {
        let mut out = Vec::with_capacity(items.len());
        for (content_id, backend_path) in items {
            let result = self.read_path(content_id, backend_path).ok();
            out.push((content_id.clone(), result));
        }
        Ok(out)
    }

    /// Read a single blob by `(content_id, backend_path)`.  Helper used
    /// by the default [`batch_read_content`](Self::batch_read_content)
    /// impl; equivalent to [`ObjectStore::read_content`] with the
    /// backend path threaded through.
    fn read_path(&self, content_id: &str, backend_path: &str) -> Result<Vec<u8>, StorageError>;

    /// Look up versions for a list of backend paths.  `None` for
    /// non-versioned backends or unknown paths.
    fn batch_get_versions(
        &self,
        backend_paths: &[String],
    ) -> Result<Vec<BatchVersionRow>, StorageError> {
        let mut out = Vec::with_capacity(backend_paths.len());
        for path in backend_paths {
            let v = self.get_version_by_path(path).unwrap_or(None);
            out.push((path.clone(), v));
        }
        Ok(out)
    }

    /// Write multiple `(content_id, content)` pairs.  Per-item failure
    /// surfaces as `None` in the returned map.
    fn batch_write_content(
        &self,
        items: Vec<(String, Vec<u8>)>,
        ctx: &kernel::kernel::OperationContext,
    ) -> Result<Vec<BatchWriteRow>, StorageError> {
        let mut out = Vec::with_capacity(items.len());
        for (cid, data) in items {
            let result = self.write_content(&data, &cid, ctx, 0).ok();
            out.push((cid, result));
        }
        Ok(out)
    }

    /// Delete multiple content-ids.  Per-item success is recorded; the
    /// call as a whole succeeds even when individual deletes fail.
    fn batch_delete_content(
        &self,
        content_ids: &[String],
    ) -> Result<Vec<BatchDeleteRow>, StorageError> {
        let mut out = Vec::with_capacity(content_ids.len());
        for cid in content_ids {
            let ok = self.delete_content(cid).is_ok();
            out.push((cid.clone(), ok));
        }
        Ok(out)
    }
}
