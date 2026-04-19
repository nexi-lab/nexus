//! Kernel ``Metastore`` impl backed by a Raft ``ZoneConsensus``.
//!
//! Federation mounts install a ``ZoneMetastore`` on the kernel's per-mount
//! ``mount_metastores`` map so ``Kernel::with_metastore(mount_point)``
//! hits the zone's Raft state machine on cold-dcache lookups. Writes
//! go through ``propose`` (Raft consensus); reads hit the local state
//! machine directly.
//!
//! Lives in the ``nexus_kernel`` crate (not ``raft``) so the direction is
//! kernel → raft via an rlib dependency. F2 C1 tried the reverse
//! (raft linking kernel rlib) and hit SIGSEGV from duplicated
//! ``thread_local!`` state across two cdylibs. C8 fix: raft is an
//! rlib inside the single ``nexus_kernel`` cdylib, so there's only
//! one copy of kernel code in the process.
//!
//! Field fidelity note: the kernel ``FileMetadata`` struct tracks a
//! subset of the proto fields (path/backend_name/physical_path/size/etag/
//! version/entry_type/zone_id/mime_type). ``target_zone_id`` lives on the
//! proto (used by federation's state machine to emit mount events) but
//! is intentionally NOT on the kernel struct — DT_MOUNT writes come
//! from federation, which authors the proto directly. Remaining missing
//! fields (``owner_id``, ``ttl_seconds`` and the
//! ``created_at``/``modified_at`` ISO-8601 strings — distinct from the
//! ``created_at_ms``/``modified_at_ms`` epoch fields already tracked)
//! still round-trip through Python-side writes fine but are defaulted on
//! kernel-only writes. Widening the kernel struct is tracked by #18.

use std::sync::Arc;

use nexus_raft::prelude::{Command, FullStateMachine, ZoneConsensus};
use nexus_raft::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
use prost::Message;

use crate::metastore::{FileMetadata as KernelFileMetadata, Metastore, MetastoreError};

/// ``kernel::Metastore`` impl backed by a single ``ZoneConsensus``.
pub struct ZoneMetastore {
    node: ZoneConsensus<FullStateMachine>,
    runtime: tokio::runtime::Handle,
}

impl ZoneMetastore {
    /// Construct from a running ``ZoneConsensus`` + its tokio runtime.
    pub fn new(node: ZoneConsensus<FullStateMachine>, runtime: tokio::runtime::Handle) -> Self {
        Self { node, runtime }
    }

    /// Return an ``Arc<dyn Metastore>`` ready to install into
    /// ``Kernel::mount_metastores``.
    pub fn new_arc(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
    ) -> Arc<dyn Metastore> {
        Arc::new(Self::new(node, runtime))
    }
}

pub(crate) fn proto_to_kernel(bytes: &[u8]) -> Result<KernelFileMetadata, MetastoreError> {
    let proto = ProtoFileMetadata::decode(bytes)
        .map_err(|e| MetastoreError::IOError(format!("FileMetadata proto decode: {e}")))?;
    Ok(KernelFileMetadata {
        path: proto.path,
        backend_name: proto.backend_name,
        physical_path: proto.physical_path,
        size: proto.size as u64,
        etag: if proto.etag.is_empty() {
            None
        } else {
            Some(proto.etag)
        },
        version: proto.version as u32,
        entry_type: proto.entry_type as u8,
        zone_id: if proto.zone_id.is_empty() {
            None
        } else {
            Some(proto.zone_id)
        },
        mime_type: if proto.mime_type.is_empty() {
            None
        } else {
            Some(proto.mime_type)
        },
        created_at_ms: None,
        modified_at_ms: None,
    })
}

pub(crate) fn kernel_to_proto(meta: &KernelFileMetadata) -> Vec<u8> {
    // ``target_zone_id`` is intentionally left at the proto default ("")
    // — the kernel struct does not carry it. DT_MOUNT writes that need
    // a target come from federation (``rust/raft/src/pyo3_bindings.rs``
    // constructs the proto directly); entries written through
    // ``ZoneMetastore`` are non-mount kinds whose target is always "".
    let proto = ProtoFileMetadata {
        path: meta.path.clone(),
        backend_name: meta.backend_name.clone(),
        physical_path: meta.physical_path.clone(),
        size: meta.size as i64,
        etag: meta.etag.clone().unwrap_or_default(),
        version: meta.version as i32,
        entry_type: meta.entry_type as i32,
        zone_id: meta.zone_id.clone().unwrap_or_default(),
        mime_type: meta.mime_type.clone().unwrap_or_default(),
        ..Default::default()
    };
    proto.encode_to_vec()
}

impl Metastore for ZoneMetastore {
    fn get(&self, path: &str) -> Result<Option<KernelFileMetadata>, MetastoreError> {
        let key = path.to_string();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.get_metadata(&key));
        let bytes_opt = self
            .runtime
            .block_on(fut)
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.get({path}): {e}")))?;
        match bytes_opt {
            Some(bytes) => {
                let bytes_vec: Vec<u8> = bytes;
                Ok(Some(proto_to_kernel(&bytes_vec)?))
            }
            None => Ok(None),
        }
    }

    fn put(&self, path: &str, metadata: KernelFileMetadata) -> Result<(), MetastoreError> {
        let value = kernel_to_proto(&metadata);
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.put({path}): {e}")))?;
        match result {
            nexus_raft::prelude::CommandResult::Success => Ok(()),
            nexus_raft::prelude::CommandResult::Error(e) => Err(MetastoreError::IOError(format!(
                "ZoneMetastore.put({path}) rejected: {e}"
            ))),
            _ => Ok(()),
        }
    }

    fn delete(&self, path: &str) -> Result<bool, MetastoreError> {
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        let result = self
            .runtime
            .block_on(self.node.propose(cmd))
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.delete({path}): {e}")))?;
        Ok(matches!(
            result,
            nexus_raft::prelude::CommandResult::Success
        ))
    }

    fn list(&self, prefix: &str) -> Result<Vec<KernelFileMetadata>, MetastoreError> {
        let key = prefix.to_string();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.list_metadata(&key));
        let entries = self
            .runtime
            .block_on(fut)
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.list({prefix}): {e}")))?;
        let mut out: Vec<KernelFileMetadata> = Vec::with_capacity(entries.len());
        for entry in entries {
            let (_k, bytes): (String, Vec<u8>) = entry;
            out.push(proto_to_kernel(&bytes)?);
        }
        Ok(out)
    }

    fn exists(&self, path: &str) -> Result<bool, MetastoreError> {
        self.get(path).map(|m| m.is_some())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Proto encode↔decode preserves every field the kernel struct
    /// tracks. ``target_zone_id`` deliberately not asserted here —
    /// R20.1 removed it from the kernel struct on the principle that
    /// federation (which authors DT_MOUNT entries) operates on the
    /// proto directly, so dropping the field from the kernel-side
    /// mapper is correct.
    #[test]
    fn proto_roundtrip_preserves_kernel_fields() {
        let meta = KernelFileMetadata {
            path: "/docs/readme.md".to_string(),
            backend_name: "local".to_string(),
            physical_path: "abc123".to_string(),
            size: 1024,
            etag: Some("hash".to_string()),
            version: 3,
            entry_type: 0, // DT_REG
            zone_id: Some("zone-a".to_string()),
            mime_type: Some("text/markdown".to_string()),
            created_at_ms: None,
            modified_at_ms: None,
        };
        let restored = proto_to_kernel(&kernel_to_proto(&meta)).unwrap();
        assert_eq!(restored.path, meta.path);
        assert_eq!(restored.backend_name, meta.backend_name);
        assert_eq!(restored.physical_path, meta.physical_path);
        assert_eq!(restored.size, meta.size);
        assert_eq!(restored.etag, meta.etag);
        assert_eq!(restored.version, meta.version);
        assert_eq!(restored.entry_type, meta.entry_type);
        assert_eq!(restored.zone_id, meta.zone_id);
        assert_eq!(restored.mime_type, meta.mime_type);
        assert_eq!(restored.created_at_ms, None);
        assert_eq!(restored.modified_at_ms, None);
    }
}
