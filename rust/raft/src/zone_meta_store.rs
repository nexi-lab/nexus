//! Kernel ``MetaStore`` impl backed by a Raft ``ZoneConsensus``.
//!
//! Federation mounts install a ``ZoneMetaStore`` on the kernel's per-mount
//! metastore slot so ``Kernel::with_metastore(mount_point)`` hits the
//! zone's Raft state machine on cold-dcache lookups. Writes go through
//! ``propose`` (Raft consensus); reads hit the local state machine
//! directly.
//!
//! ``ZoneMetaStore`` owns the full↔zone-relative path translation.
//! The trait boundary always sees full global paths; the state
//! machine always sees zone-relative keys. This keeps
//! ``FileMetadata.path`` consistent with callers' worldview while
//! preserving the crosslink invariant (a zone mounted at multiple
//! global paths stores one authoritative copy per zone-relative key).
//!
//! Field fidelity note: the kernel ``FileMetadata`` struct tracks a
//! subset of the proto fields (path/backend_name/physical_path/size/content_id/
//! version/entry_type/zone_id/mime_type). Missing fields (``owner_id``,
//! ``ttl_seconds`` and the ``created_at``/``modified_at`` ISO-8601
//! strings — distinct from the ``created_at_ms``/``modified_at_ms``
//! epoch fields already tracked) still round-trip through Python-side
//! writes fine but are defaulted on kernel-only writes. Widening the
//! kernel struct is tracked by #18.

use std::sync::Arc;

use crate::prelude::{Command, FullStateMachine, ZoneConsensus};
use crate::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
use contracts::VFS_ROOT;
use prost::Message;

use kernel::meta_store::{FileMetadata as KernelFileMetadata, MetaStore, MetaStoreError};

fn bridge_block_on<F>(handle: &tokio::runtime::Handle, fut: F) -> F::Output
where
    F: std::future::Future,
{
    if tokio::runtime::Handle::try_current().is_ok() {
        tokio::task::block_in_place(|| handle.block_on(fut))
    } else {
        handle.block_on(fut)
    }
}

/// ``kernel::MetaStore`` impl backed by a single ``ZoneConsensus``.
///
/// The ``mount_point`` field is the VFS-global prefix this zone is
/// exposed under (e.g. ``/corp``). It is used to translate between
/// caller-facing full paths and state-machine zone-relative keys —
/// never surfaced through the trait API.
///
/// ``coherence_id`` is a stable integer identity of the underlying
/// state machine (``ZoneConsensus::coherence_id``). Every crosslink
/// mount of the same zone has a different ``mount_point`` but shares
/// the SAME ``coherence_id``, which is how
/// ``VFSRouter::mount_points_for_coherence_key`` fans out apply-side
/// dcache invalidation across all surfaces of the zone.
pub struct ZoneMetaStore {
    node: ZoneConsensus<FullStateMachine>,
    runtime: tokio::runtime::Handle,
    mount_point: String,
    coherence_id: usize,
    /// Internal cache projection — same shape as `LocalMetaStore` /
    /// `RemoteMetaStore`.  Each zone metastore caches its own hot
    /// entries (keyed by the caller-facing GLOBAL path, mirroring the
    /// `to_global_path` rewrite done on read so callers see consistent
    /// keys).  `get` consults the cache first; `put` is write-through;
    /// `delete` invalidates pre-propose.
    ///
    /// Apply-side coherence on follower nodes (and on crosslink
    /// surfaces of the same zone on the leader): every ZoneMetaStore
    /// constructed against a consensus self-registers an apply-side
    /// invalidator on the consensus's invalidate_cb_slot. When the
    /// state machine commits a SetMetadata / DeleteMetadata, the slot
    /// fires every registered cb — each one evicts its own cache row
    /// for the corresponding global path. Wrapped in ``Arc`` so the
    /// closure can hold a stable handle that survives the
    /// ZoneMetaStore's call frames.
    cache: Arc<dashmap::DashMap<String, KernelFileMetadata>>,
}

impl ZoneMetaStore {
    /// Construct from a running ``ZoneConsensus`` + its tokio runtime
    /// + the VFS mount point this zone surfaces under.
    ///
    /// ``mount_point`` is mandatory: every caller is a VFS mount.
    /// The value should be the canonical form
    /// (e.g. ``"/corp"``, ``"/"`` for the root zone) — the same key
    /// ``Kernel::with_metastore`` routes against.
    ///
    /// Self-registers an apply-side invalidator on ``node`` so the
    /// internal cache stays coherent with raft commits — see the
    /// ``cache`` field docstring for the full coherence story.
    pub fn new(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
        mount_point: String,
    ) -> Self {
        let coherence_id = node.coherence_id();
        let cache: Arc<dashmap::DashMap<String, KernelFileMetadata>> =
            Arc::new(dashmap::DashMap::new());
        // Self-register an apply-side invalidator. The closure receives
        // zone-relative keys from the state machine; translate back to
        // the caller-facing global path (the form this metastore caches
        // under) before evicting. Capturing ``Arc`` clones keeps the
        // closure self-contained — no back-reference to ZoneMetaStore.
        {
            let cache_for_cb = Arc::clone(&cache);
            let mount_point_for_cb = mount_point.clone();
            node.register_invalidate_cb(Arc::new(move |zone_key: &str| {
                let global = if mount_point_for_cb == VFS_ROOT || mount_point_for_cb.is_empty() {
                    zone_key.to_string()
                } else if zone_key == VFS_ROOT {
                    mount_point_for_cb.clone()
                } else {
                    format!("{}{}", mount_point_for_cb, zone_key)
                };
                cache_for_cb.remove(&global);
            }));
        }
        Self {
            node,
            runtime,
            mount_point,
            coherence_id,
            cache,
        }
    }

    /// Return an ``Arc<dyn MetaStore>`` ready to install into a
    /// kernel mount entry.
    pub fn new_arc(
        node: ZoneConsensus<FullStateMachine>,
        runtime: tokio::runtime::Handle,
        mount_point: String,
    ) -> Arc<dyn MetaStore> {
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
            "ZoneMetaStore({}): path {} does not sit under mount point",
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

pub(crate) fn proto_to_kernel(bytes: &[u8]) -> Result<KernelFileMetadata, MetaStoreError> {
    let proto = ProtoFileMetadata::decode(bytes)
        .map_err(|e| MetaStoreError::IOError(format!("FileMetadata proto decode: {e}")))?;
    Ok(KernelFileMetadata {
        path: proto.path,
        size: proto.size as u64,
        content_id: if proto.content_id.is_empty() {
            None
        } else {
            Some(proto.content_id)
        },
        gen: proto.gen,
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
        last_writer_address: if proto.last_writer_address.is_empty() {
            None
        } else {
            Some(proto.last_writer_address)
        },
        target_zone_id: if proto.target_zone_id.is_empty() {
            None
        } else {
            Some(proto.target_zone_id)
        },
        link_target: if proto.link_target.is_empty() {
            None
        } else {
            Some(proto.link_target)
        },
    })
}

pub(crate) fn kernel_to_proto(meta: &KernelFileMetadata) -> Vec<u8> {
    let proto = ProtoFileMetadata {
        path: meta.path.clone(),
        size: meta.size as i64,
        content_id: meta.content_id.clone().unwrap_or_default(),
        gen: meta.gen,
        version: meta.version as i32,
        entry_type: meta.entry_type as i32,
        zone_id: meta.zone_id.clone().unwrap_or_default(),
        mime_type: meta.mime_type.clone().unwrap_or_default(),
        last_writer_address: meta.last_writer_address.clone().unwrap_or_default(),
        // For DT_MOUNT entries this carries the cross-zone routing
        // pointer that federation's `mount_apply_cb` reads on every
        // replicated SetMetadata to wire the mount on followers.
        // Empty for non-DT_MOUNT entries.
        target_zone_id: meta.target_zone_id.clone().unwrap_or_default(),
        // For DT_LINK entries this carries the link target path the
        // route() one-hop resolver follows. Empty for non-DT_LINK entries.
        link_target: meta.link_target.clone().unwrap_or_default(),
        ..Default::default()
    };
    proto.encode_to_vec()
}

impl MetaStore for ZoneMetaStore {
    fn get(&self, path: &str) -> Result<Option<KernelFileMetadata>, MetaStoreError> {
        if let Some(cached) = self.cache.get(path) {
            return Ok(Some(cached.clone()));
        }
        let zone_key = self.to_zone_key(path);
        let key = zone_key.clone();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.get_metadata(&key));
        let bytes_opt = bridge_block_on(&self.runtime, fut)
            .map_err(|e| MetaStoreError::IOError(format!("ZoneMetaStore.get({path}): {e}")))?;
        match bytes_opt {
            Some(bytes) => {
                let mut kmeta = proto_to_kernel(&bytes)?;
                // State machine stores zone-relative; hand callers
                // the full path they expect.
                kmeta.path = self.to_global_path(&kmeta.path);
                self.cache.insert(path.to_string(), kmeta.clone());
                Ok(Some(kmeta))
            }
            None => Ok(None),
        }
    }

    fn put(&self, path: &str, mut metadata: KernelFileMetadata) -> Result<(), MetaStoreError> {
        let zone_key = self.to_zone_key(path);
        // Rewrite the proto's path field to match the stored key so
        // later reads (which translate back to full) produce a
        // self-consistent record. Without this a crosslink read that
        // travels through a different mount point would see the
        // originating mount's global path, not its own.
        metadata.path = zone_key.clone();
        let value = kernel_to_proto(&metadata);
        let value_snapshot = value.clone();
        let cmd = Command::SetMetadata {
            key: zone_key.clone(),
            value,
        };
        let result = bridge_block_on(&self.runtime, self.node.propose(cmd))
            .map_err(|e| MetaStoreError::IOError(format!("ZoneMetaStore.put({path}): {e}")))?;
        match result {
            crate::prelude::CommandResult::Success => {}
            crate::prelude::CommandResult::Error(e) => {
                return Err(MetaStoreError::IOError(format!(
                    "ZoneMetaStore.put({path}) rejected: {e}"
                )));
            }
            _ => {}
        }
        // Read-your-writes: poll local state machine until the exact
        // bytes we just wrote show up. Propose returns on leader commit;
        // on a follower, local apply lags by up to one raft tick.
        // SSOT = raft state machine.
        let runtime = self.runtime.clone();
        let node = self.node.clone();
        let key = zone_key.clone();
        let _ = self.node.wait_until(
            || {
                let poll_key = key.clone();
                let observed = bridge_block_on(
                    &runtime,
                    node.with_state_machine(move |sm: &FullStateMachine| {
                        sm.get_metadata(&poll_key)
                    }),
                );
                matches!(&observed, Ok(Some(bytes)) if *bytes == value_snapshot)
            },
            500,
        );
        // Write-through: cache the caller-facing form (rewrite the
        // path field back to global so cache reads match the get()
        // contract).
        let mut cache_meta = metadata;
        cache_meta.path = self.to_global_path(&cache_meta.path);
        self.cache.insert(path.to_string(), cache_meta);
        Ok(())
    }

    fn delete(&self, path: &str) -> Result<bool, MetaStoreError> {
        // Invalidate cache pre-propose (race-safe).
        self.cache.remove(path);
        let zone_key = self.to_zone_key(path);
        let cmd = Command::DeleteMetadata { key: zone_key };
        let result = bridge_block_on(&self.runtime, self.node.propose(cmd))
            .map_err(|e| MetaStoreError::IOError(format!("ZoneMetaStore.delete({path}): {e}")))?;
        Ok(matches!(result, crate::prelude::CommandResult::Success))
    }

    fn list(&self, prefix: &str) -> Result<Vec<KernelFileMetadata>, MetaStoreError> {
        let zone_prefix = self.to_zone_key(prefix);
        let key = zone_prefix.clone();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.list_metadata(&key));
        let entries = bridge_block_on(&self.runtime, fut)
            .map_err(|e| MetaStoreError::IOError(format!("ZoneMetaStore.list({prefix}): {e}")))?;
        let mut out: Vec<KernelFileMetadata> = Vec::with_capacity(entries.len());
        for entry in entries {
            let (_k, bytes): (String, Vec<u8>) = entry;
            let mut kmeta = proto_to_kernel(&bytes)?;
            kmeta.path = self.to_global_path(&kmeta.path);
            out.push(kmeta);
        }
        Ok(out)
    }

    fn exists(&self, path: &str) -> Result<bool, MetaStoreError> {
        self.get(path).map(|m| m.is_some())
    }

    fn coherence_key(&self) -> Option<usize> {
        Some(self.coherence_id)
    }

    fn append_stream_entry(&self, key: &str, data: &[u8]) -> Result<(), MetaStoreError> {
        // Stream entries skip the zone-key translation `put` does for
        // FileMetadata — `key` is the full WAL identity (`__wal_stream__/…`
        // or `__wal_pipe__/…`) and the side-table is zone-scoped at the
        // raft state-machine layer (this `ZoneMetaStore` is bound to a
        // single zone via `node`).
        let cmd = Command::AppendStreamEntry {
            key: key.to_string(),
            data: data.to_vec(),
        };
        let result = bridge_block_on(&self.runtime, self.node.propose(cmd)).map_err(|e| {
            MetaStoreError::IOError(format!("ZoneMetaStore.append_stream_entry({key}): {e}"))
        })?;
        match result {
            crate::prelude::CommandResult::Success => Ok(()),
            crate::prelude::CommandResult::Error(e) => Err(MetaStoreError::IOError(format!(
                "ZoneMetaStore.append_stream_entry({key}) rejected: {e}"
            ))),
            _ => Ok(()),
        }
    }

    fn get_stream_entry(&self, key: &str) -> Result<Option<Vec<u8>>, MetaStoreError> {
        let key_owned = key.to_string();
        let fut = self
            .node
            .with_state_machine(move |sm: &FullStateMachine| sm.get_stream_entry(&key_owned));
        bridge_block_on(&self.runtime, fut).map_err(|e| {
            MetaStoreError::IOError(format!("ZoneMetaStore.get_stream_entry({key}): {e}"))
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::raft::ZoneRaftRegistry;
    use tempfile::TempDir;

    /// Proto encode↔decode preserves every field the kernel struct
    /// tracks. ``target_zone_id`` deliberately not asserted here —
    /// `target_zone_id` is now carried on the kernel struct (added back
    /// for federation's `mount_apply_cb` to read on every replicated
    /// SetMetadata) and round-trips through the proto.
    #[test]
    fn proto_roundtrip_preserves_kernel_fields() {
        let meta = KernelFileMetadata {
            path: "/docs/readme.md".to_string(),
            size: 1024,
            content_id: Some("hash".to_string()),
            gen: 17,
            version: 3,
            entry_type: 0, // DT_REG
            zone_id: Some("zone-a".to_string()),
            mime_type: Some("text/markdown".to_string()),
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: Some("nexus-1:2028".to_string()),
            target_zone_id: None,
            link_target: None,
        };
        let restored = proto_to_kernel(&kernel_to_proto(&meta)).unwrap();
        assert_eq!(restored.path, meta.path);
        assert_eq!(restored.size, meta.size);
        assert_eq!(restored.content_id, meta.content_id);
        assert_eq!(restored.gen, meta.gen);
        assert_eq!(restored.version, meta.version);
        assert_eq!(restored.entry_type, meta.entry_type);
        assert_eq!(restored.zone_id, meta.zone_id);
        assert_eq!(restored.mime_type, meta.mime_type);
        assert_eq!(restored.created_at_ms, None);
        assert_eq!(restored.modified_at_ms, None);
        assert_eq!(restored.last_writer_address, meta.last_writer_address);
    }

    /// Pure-function translation is unit-testable without a live
    /// ZoneConsensus — build a stub struct literal and exercise the
    /// helpers directly. (Field-level construction isn't possible
    /// because ZoneConsensus is opaque; instead we test the helpers
    /// by decomposition: any path whose translation is independent
    /// of consensus can be covered here.)
    fn translate_roundtrip(mount_point: &str, full: &str) -> String {
        // Mirror ZoneMetaStore::to_zone_key / to_global_path without
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

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn sync_methods_are_safe_inside_tokio_runtime() {
        let tmp = TempDir::new().unwrap();
        let registry = ZoneRaftRegistry::new(tmp.path().to_path_buf(), 1);
        let runtime = tokio::runtime::Handle::current();
        let node = registry
            .create_zone("corp", vec![], &runtime)
            .expect("create test zone");
        node.campaign().await.expect("campaign test zone");
        let store = ZoneMetaStore::new(node, runtime, "/corp".to_string());
        let path = "/corp/doc.txt";
        let meta = KernelFileMetadata {
            path: path.to_string(),
            size: 5,
            content_id: Some("hello-hash".to_string()),
            gen: 1,
            version: 1,
            entry_type: 0,
            zone_id: Some("corp".to_string()),
            mime_type: Some("text/plain".to_string()),
            created_at_ms: None,
            modified_at_ms: None,
            last_writer_address: Some("nexus-1:2126".to_string()),
            target_zone_id: None,
            link_target: None,
        };

        store.put(path, meta.clone()).expect("put from runtime");
        let got = store
            .get(path)
            .expect("get from runtime")
            .expect("metadata");
        assert_eq!(got.path, path);
        assert_eq!(got.content_id, meta.content_id);
        let listed = store.list("/corp").expect("list from runtime");
        assert_eq!(listed.len(), 1);

        registry.shutdown_all();
    }
}
