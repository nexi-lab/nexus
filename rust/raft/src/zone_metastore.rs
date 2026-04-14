//! Kernel `Metastore` bridge backed by a Raft `ZoneConsensus` state machine.
//!
//! In federation mode each zone owns an independent Raft group whose state
//! machine persists serialized `FileMetadata` protobuf blobs. Standalone
//! mode uses `nexus_kernel::metastore::RedbMetastore` directly. This module
//! plumbs the Raft path into the kernel's `Metastore` trait so syscalls
//! (sys_read, sys_stat, sys_readdir, …) can fall back to the correct
//! per-zone store on dcache miss via `kernel.with_metastore(mount_point)`.
//!
//! Wiring:
//!   Python `MountTable.add()` → `kernel.add_mount(py_metastore=…)` →
//!   `Kernel::mount_metastores.insert(canonical, Arc<ZoneMetastore>)`.
//!
//! The value bytes stored in the state machine are `prost`-encoded
//! `nexus::core::FileMetadata` (same SSOT proto used by Python via
//! `nexus.storage._metadata_mapper_generated`). Decoding on read,
//! encoding on write. Writes go through Raft consensus via `propose`.
//!
//! Field fidelity: the kernel `FileMetadata` struct currently tracks a
//! subset of the proto fields (path/backend_name/physical_path/size/etag/
//! version/entry_type/zone_id/mime_type). On kernel-initiated writes,
//! the missing fields (`created_at`, `modified_at`, `owner_id`,
//! `target_zone_id`, `ttl_seconds`) are defaulted — they round-trip through
//! Python-side writes fine, but a Rust-kernel-only write path would not
//! preserve them. Widening `nexus_kernel::metastore::FileMetadata` is a
//! separate task.

use std::sync::Arc;

use nexus_kernel::metastore::{FileMetadata as KernelFileMetadata, Metastore, MetastoreError};
use prost::Message;

use crate::prelude::{Command, FullStateMachine, ZoneConsensus};
use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;

/// `kernel::Metastore` impl backed by a single `ZoneConsensus` state machine.
///
/// Clones the underlying `ZoneConsensus` (cheap — `Arc`-based internally)
/// and holds a `tokio::runtime::Handle` to bridge the sync `Metastore` API
/// onto Raft's async propose/state-machine access.
pub struct ZoneMetastore {
    node: ZoneConsensus<FullStateMachine>,
    runtime: tokio::runtime::Handle,
}

impl ZoneMetastore {
    /// Construct a new metastore bridge from a running zone and its runtime.
    pub fn new(node: ZoneConsensus<FullStateMachine>, runtime: tokio::runtime::Handle) -> Self {
        Self { node, runtime }
    }

    /// Convenience constructor that returns an `Arc<dyn Metastore>` ready
    /// to be handed to `Kernel::mount_metastores`.
    pub fn new_arc(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
    ) -> Arc<dyn Metastore> {
        Arc::new(Self::new(node, runtime))
    }
}

// ── Proto ↔ Kernel FileMetadata conversion ─────────────────────────────────
//
// SSOT: proto/nexus/core/metadata.proto. Python uses the same proto via
// `nexus.storage._metadata_mapper_generated.MetadataMapper`; both sides
// must stay compatible at the wire format.

fn proto_to_kernel(p: ProtoFileMetadata) -> KernelFileMetadata {
    KernelFileMetadata {
        path: p.path,
        backend_name: p.backend_name,
        physical_path: p.physical_path,
        size: p.size as u64,
        etag: none_if_empty(p.etag),
        version: p.version as u32,
        entry_type: p.entry_type as u8,
        zone_id: none_if_empty(p.zone_id),
        mime_type: none_if_empty(p.mime_type),
    }
}

fn kernel_to_proto(k: KernelFileMetadata) -> ProtoFileMetadata {
    ProtoFileMetadata {
        path: k.path,
        backend_name: k.backend_name,
        physical_path: k.physical_path,
        size: k.size as i64,
        etag: k.etag.unwrap_or_default(),
        mime_type: k.mime_type.unwrap_or_default(),
        // Fields not tracked by kernel FileMetadata — defaulted on write.
        // Python-initiated writes populate these via the proto path;
        // kernel-initiated writes (sys_write) will clear them. See module
        // doc "Field fidelity" note.
        created_at: String::new(),
        modified_at: String::new(),
        version: k.version as i32,
        zone_id: k.zone_id.unwrap_or_default(),
        owner_id: String::new(),
        entry_type: k.entry_type as i32,
        target_zone_id: String::new(),
        ttl_seconds: 0.0,
    }
}

fn none_if_empty(s: String) -> Option<String> {
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

// ── Metastore impl ─────────────────────────────────────────────────────────

impl Metastore for ZoneMetastore {
    fn get(&self, path: &str) -> Result<Option<KernelFileMetadata>, MetastoreError> {
        let node = self.node.clone();
        let path_s = path.to_string();
        let bytes_opt: Option<Vec<u8>> = self
            .runtime
            .block_on(async move { node.with_state_machine(|sm| sm.get_metadata(&path_s)).await })
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.get({path}): {e}")))?;

        match bytes_opt {
            Some(bytes) => {
                let proto = ProtoFileMetadata::decode(bytes.as_slice()).map_err(|e| {
                    MetastoreError::IOError(format!("ZoneMetastore.get({path}): proto decode: {e}"))
                })?;
                Ok(Some(proto_to_kernel(proto)))
            }
            None => Ok(None),
        }
    }

    fn put(&self, path: &str, metadata: KernelFileMetadata) -> Result<(), MetastoreError> {
        let proto = kernel_to_proto(metadata);
        let value = proto.encode_to_vec();
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        let node = self.node.clone();
        self.runtime
            .block_on(async move { node.propose(cmd).await })
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.put({path}): {e}")))?;
        Ok(())
    }

    fn delete(&self, path: &str) -> Result<bool, MetastoreError> {
        // Check existence up front so we can return the boolean the trait expects.
        // The Raft delete is idempotent, so racing a concurrent delete is fine.
        let existed = self.exists(path)?;
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        let node = self.node.clone();
        self.runtime
            .block_on(async move { node.propose(cmd).await })
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.delete({path}): {e}")))?;
        Ok(existed)
    }

    fn list(&self, prefix: &str) -> Result<Vec<KernelFileMetadata>, MetastoreError> {
        let node = self.node.clone();
        let prefix_s = prefix.to_string();
        let entries: Vec<(String, Vec<u8>)> = self
            .runtime
            .block_on(async move {
                node.with_state_machine(|sm| sm.list_metadata(&prefix_s))
                    .await
            })
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.list({prefix}): {e}")))?;

        let mut out = Vec::with_capacity(entries.len());
        for (_, bytes) in entries {
            let proto = ProtoFileMetadata::decode(bytes.as_slice()).map_err(|e| {
                MetastoreError::IOError(format!("ZoneMetastore.list({prefix}): proto decode: {e}"))
            })?;
            out.push(proto_to_kernel(proto));
        }
        Ok(out)
    }

    fn exists(&self, path: &str) -> Result<bool, MetastoreError> {
        Ok(self.get(path)?.is_some())
    }
}
