//! Kernel ``Metastore`` impl backed by a Raft ``ZoneConsensus``.
//!
//! Federation mounts install a ``ZoneMetastore`` on the kernel's per-mount
//! metastore slot so ``Kernel::with_metastore(mount_point)`` hits the
//! zone's Raft state machine on cold-dcache lookups. Writes go through
//! ``propose`` (Raft consensus); reads hit the local state machine
//! directly.
//!
//! R20.3: ``ZoneMetastore`` owns the full↔zone-relative path
//! translation. The trait boundary always sees full global paths; the
//! state machine always sees zone-relative keys. This keeps
//! ``FileMetadata.path`` consistent with callers' worldview while
//! preserving the crosslink invariant (a zone mounted at multiple
//! global paths stores one authoritative copy per zone-relative key).
//!
//! Field fidelity note: the kernel ``FileMetadata`` struct tracks a
//! subset of the proto fields (path/backend_name/physical_path/size/etag/
//! version/entry_type/zone_id/mime_type). Missing fields (``owner_id``,
//! ``ttl_seconds`` and the ``created_at``/``modified_at`` ISO-8601
//! strings — distinct from the ``created_at_ms``/``modified_at_ms``
//! epoch fields already tracked) still round-trip through Python-side
//! writes fine but are defaulted on kernel-only writes. Widening the
//! kernel struct is tracked by #18.

use std::sync::Arc;

use contracts::VFS_ROOT;
use nexus_raft::prelude::{Command, FullStateMachine, ZoneConsensus};
use nexus_raft::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
use prost::Message;

use crate::metastore::{FileMetadata as KernelFileMetadata, Metastore, MetastoreError};

/// ``kernel::Metastore`` impl backed by a single ``ZoneConsensus``.
///
/// The ``mount_point`` field is the VFS-global prefix this zone is
/// exposed under (e.g. ``/corp``). It is used to translate between
/// caller-facing full paths and state-machine zone-relative keys —
/// never surfaced through the trait API.
pub struct ZoneMetastore {
    node: ZoneConsensus<FullStateMachine>,
    runtime: tokio::runtime::Handle,
    mount_point: String,
}

impl ZoneMetastore {
    /// Construct from a running ``ZoneConsensus`` + its tokio runtime
    /// + the VFS mount point this zone surfaces under.
    ///
    /// ``mount_point`` is mandatory post-R19.1b': every remaining
    /// caller is a VFS mount (WAL streams took the non-VFS escape
    /// hatch out). The value should be the canonical form
    /// (e.g. ``"/corp"``, ``"/"`` for the root zone) — the same key
    /// ``Kernel::with_metastore`` routes against.
    pub fn new(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
        mount_point: String,
    ) -> Self {
        Self {
            node,
            runtime,
            mount_point,
        }
    }

    /// Return an ``Arc<dyn Metastore>`` ready to install into a
    /// kernel mount entry.
    pub fn new_arc(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
        mount_point: String,
    ) -> Arc<dyn Metastore> {
        Arc::new(Self::new(node, runtime, mount_point))
    }

    /// Full caller-facing path → zone-relative state-machine key.
    ///
    /// ``/`` when the full path equals the mount point (root of the
    /// zone). Otherwise strips the mount prefix and re-anchors at
    /// ``/``. Paths that don't start with the mount point indicate a
    /// caller bug — we ``debug_assert`` to catch the mistake in tests
    /// and return the path unchanged in release (never silently
    /// corrupt storage by rewriting an unrelated prefix).
    fn to_zone_key(&self, full_path: &str) -> String {
        if self.mount_point == VFS_ROOT || self.mount_point.is_empty() {
            // Root zone: the mount prefix is (effectively) empty, so
            // full paths already match the zone namespace.
            return full_path.to_string();
        }
        if full_path == self.mount_point {
            return VFS_ROOT.to_string();
        }
        let with_trailing = format!("{}/", self.mount_point);
        if let Some(rest) = full_path.strip_prefix(&with_trailing) {
            return format!("/{}", rest);
        }
        debug_assert!(
            false,
            "ZoneMetastore({}): path {} does not sit under mount point",
            self.mount_point, full_path
        );
        full_path.to_string()
    }

    /// Zone-relative state-machine key → full caller-facing path.
    fn to_global_path(&self, zone_key: &str) -> String {
        if self.mount_point == VFS_ROOT || self.mount_point.is_empty() {
            return zone_key.to_string();
        }
        if zone_key == VFS_ROOT {
            return self.mount_point.clone();
        }
        // zone_key begins with '/'; avoid double slash.
        format!("{}{}", self.mount_point, zone_key)
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
        let zone_key = self.to_zone_key(path);
        let key = zone_key.clone();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.get_metadata(&key));
        let bytes_opt = self
            .runtime
            .block_on(fut)
            .map_err(|e| MetastoreError::IOError(format!("ZoneMetastore.get({path}): {e}")))?;
        match bytes_opt {
            Some(bytes) => {
                let mut kmeta = proto_to_kernel(&bytes)?;
                // State machine stores zone-relative; hand callers
                // the full path they expect.
                kmeta.path = self.to_global_path(&kmeta.path);
                Ok(Some(kmeta))
            }
            None => Ok(None),
        }
    }

    fn put(&self, path: &str, mut metadata: KernelFileMetadata) -> Result<(), MetastoreError> {
        let zone_key = self.to_zone_key(path);
        // Rewrite the proto's path field to match the stored key so
        // later reads (which translate back to full) produce a
        // self-consistent record. Without this a crosslink read that
        // travels through a different mount point would see the
        // originating mount's global path, not its own.
        metadata.path = zone_key.clone();
        let value = kernel_to_proto(&metadata);
        let cmd = Command::SetMetadata {
            key: zone_key.clone(),
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
        let zone_key = self.to_zone_key(path);
        let cmd = Command::DeleteMetadata { key: zone_key };
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
        let zone_prefix = self.to_zone_key(prefix);
        let key = zone_prefix.clone();
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
            let mut kmeta = proto_to_kernel(&bytes)?;
            kmeta.path = self.to_global_path(&kmeta.path);
            out.push(kmeta);
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

    /// R20.3: pure-function translation is unit-testable without a
    /// live ZoneConsensus — build a stub struct literal and exercise
    /// the helpers directly. (Field-level construction isn't possible
    /// because ZoneConsensus is opaque; instead we test the helpers
    /// by decomposition: any path whose translation is independent
    /// of consensus can be covered here.)
    fn translate_roundtrip(mount_point: &str, full: &str) -> String {
        // Mirror ZoneMetastore::to_zone_key / to_global_path without
        // constructing a live node.
        let zone_key = if mount_point == "/" || mount_point.is_empty() {
            full.to_string()
        } else if full == mount_point {
            "/".to_string()
        } else {
            let with_trailing = format!("{}/", mount_point);
            full.strip_prefix(&with_trailing)
                .map(|r| format!("/{}", r))
                .unwrap_or_else(|| full.to_string())
        };
        if mount_point == "/" || mount_point.is_empty() {
            zone_key
        } else if zone_key == "/" {
            mount_point.to_string()
        } else {
            format!("{}{}", mount_point, zone_key)
        }
    }

    #[test]
    fn translate_nested_mount_roundtrip() {
        // Typical federation layout: /corp mount, file at /corp/eng/readme.md
        assert_eq!(
            translate_roundtrip("/corp", "/corp/eng/readme.md"),
            "/corp/eng/readme.md"
        );
        // Mount root itself
        assert_eq!(translate_roundtrip("/corp", "/corp"), "/corp");
    }

    #[test]
    fn translate_root_mount_is_identity() {
        // Root zone uses "/" — translation is a no-op.
        assert_eq!(translate_roundtrip("/", "/foo/bar"), "/foo/bar");
        assert_eq!(translate_roundtrip("/", "/"), "/");
    }

    #[test]
    fn translate_deeply_nested_mount() {
        // Crosslink case: /family/work mount also points at same zone
        assert_eq!(
            translate_roundtrip("/family/work", "/family/work/doc.txt"),
            "/family/work/doc.txt"
        );
        assert_eq!(
            translate_roundtrip("/family/work", "/family/work"),
            "/family/work"
        );
    }
}
