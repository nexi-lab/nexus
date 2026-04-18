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
//! version/entry_type/zone_id/target_zone_id/mime_type). ``target_zone_id``
//! was added in R16.1a so DT_MOUNT entries round-trip through Rust; the
//! remaining missing fields (``owner_id``, ``ttl_seconds`` and the
//! ``created_at``/``modified_at`` ISO-8601 strings — distinct from the
//! ``created_at_ms``/``modified_at_ms`` epoch fields already tracked)
//! still round-trip through Python-side writes fine but are defaulted on
//! kernel-only writes. Widening ``kernel::metastore::FileMetadata`` to
//! cover those is tracked by follow-up #16.

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
        target_zone_id: if proto.target_zone_id.is_empty() {
            None
        } else {
            Some(proto.target_zone_id)
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
    let proto = ProtoFileMetadata {
        path: meta.path.clone(),
        backend_name: meta.backend_name.clone(),
        physical_path: meta.physical_path.clone(),
        size: meta.size as i64,
        etag: meta.etag.clone().unwrap_or_default(),
        version: meta.version as i32,
        entry_type: meta.entry_type as i32,
        zone_id: meta.zone_id.clone().unwrap_or_default(),
        target_zone_id: meta.target_zone_id.clone().unwrap_or_default(),
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

    fn mk_mount_entry() -> KernelFileMetadata {
        KernelFileMetadata {
            path: "/mnt/peer".to_string(),
            backend_name: "federation".to_string(),
            physical_path: String::new(),
            size: 0,
            etag: None,
            version: 1,
            entry_type: 2, // DT_MOUNT
            zone_id: Some("zone-a".to_string()),
            target_zone_id: Some("zone-b".to_string()),
            mime_type: None,
            created_at_ms: None,
            modified_at_ms: None,
        }
    }

    /// R16.1a byte-compat guard: a DT_MOUNT entry must preserve
    /// ``target_zone_id`` across a proto encode→decode round-trip so
    /// Rust-authored federation mounts don't silently drop the target
    /// zone Python readers rely on.
    #[test]
    fn roundtrip_mount_entry_preserves_target_zone_id() {
        let original = mk_mount_entry();
        let bytes = kernel_to_proto(&original);
        let restored = proto_to_kernel(&bytes).expect("decode must succeed");

        assert_eq!(restored.path, original.path);
        assert_eq!(restored.backend_name, original.backend_name);
        assert_eq!(restored.physical_path, original.physical_path);
        assert_eq!(restored.size, original.size);
        assert_eq!(restored.etag, original.etag);
        assert_eq!(restored.version, original.version);
        assert_eq!(restored.entry_type, original.entry_type);
        assert_eq!(restored.zone_id, original.zone_id);
        assert_eq!(restored.target_zone_id, original.target_zone_id);
        assert_eq!(restored.mime_type, original.mime_type);
        assert_eq!(restored.created_at_ms, None);
        assert_eq!(restored.modified_at_ms, None);
    }

    /// Empty ``target_zone_id`` maps to ``None`` (proto3 default),
    /// matching ``MetadataMapper.from_proto`` on the Python side.
    #[test]
    fn roundtrip_non_mount_entry_has_none_target_zone_id() {
        let mut meta = mk_mount_entry();
        meta.entry_type = 0; // DT_REG
        meta.target_zone_id = None;
        let bytes = kernel_to_proto(&meta);
        let restored = proto_to_kernel(&bytes).expect("decode must succeed");
        assert_eq!(restored.target_zone_id, None);
    }
}
