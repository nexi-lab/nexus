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
//! version/entry_type/zone_id/mime_type). Missing fields (``created_at``,
//! ``modified_at``, ``owner_id``, ``target_zone_id``, ``ttl_seconds``)
//! round-trip through Python-side writes fine but are defaulted on
//! kernel-only writes. Widening ``kernel::metastore::FileMetadata`` is
//! tracked separately.

use std::sync::Arc;

use nexus_raft::prelude::{
    Command, CommandResult, FullStateMachine, LockInfo as RaftLockInfo, LockMode as RaftLockMode,
    ZoneConsensus,
};
use nexus_raft::transport::proto::nexus::core::FileMetadata as ProtoFileMetadata;
use prost::Message;

use crate::metastore::{
    FileMetadata as KernelFileMetadata, KernelHolderInfo, KernelLockInfo, KernelLockMode,
    Metastore, MetastoreError,
};

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

fn proto_to_kernel(bytes: &[u8]) -> Result<KernelFileMetadata, MetastoreError> {
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

fn kernel_mode_to_raft(m: KernelLockMode) -> RaftLockMode {
    match m {
        KernelLockMode::Exclusive => RaftLockMode::Exclusive,
        KernelLockMode::Shared => RaftLockMode::Shared,
    }
}

fn raft_mode_to_kernel(m: RaftLockMode) -> KernelLockMode {
    match m {
        RaftLockMode::Exclusive => KernelLockMode::Exclusive,
        RaftLockMode::Shared => KernelLockMode::Shared,
    }
}

fn raft_lock_to_kernel(lock: RaftLockInfo) -> KernelLockInfo {
    KernelLockInfo {
        path: lock.path,
        max_holders: lock.max_holders,
        holders: lock
            .holders
            .into_iter()
            .map(|h| KernelHolderInfo {
                lock_id: h.lock_id,
                holder_info: h.holder_info,
                mode: raft_mode_to_kernel(h.mode),
                acquired_at_secs: h.acquired_at,
                expires_at_secs: h.expires_at,
            })
            .collect(),
    }
}

fn kernel_to_proto(meta: &KernelFileMetadata) -> Vec<u8> {
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

    // ── Advisory locks (F4 C3) ────────────────────────────────
    //
    // Writes go through the raft state machine (`propose`). Reads
    // hit the local replica via `with_state_machine`. Conflict
    // rules live in `state_machine.rs` — this forwarder only
    // translates between the kernel-native types and the raft
    // types.

    fn acquire_lock(
        &self,
        path: &str,
        lock_id: &str,
        mode: KernelLockMode,
        max_holders: u32,
        ttl_secs: u64,
        holder_info: &str,
    ) -> Result<bool, MetastoreError> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            // Raft `ttl_secs` is u32 — clamp defensively. Python
            // callers top out in the minutes/hours range so this
            // is a no-op in practice.
            ttl_secs: ttl_secs.min(u32::MAX as u64) as u32,
            holder_info: holder_info.to_string(),
            mode: kernel_mode_to_raft(mode),
            now_secs: FullStateMachine::now(),
        };
        let result = self.runtime.block_on(self.node.propose(cmd)).map_err(|e| {
            MetastoreError::IOError(format!("ZoneMetastore.acquire_lock({path}): {e}"))
        })?;
        match result {
            CommandResult::LockResult(state) => Ok(state.acquired),
            CommandResult::Error(e) => Err(MetastoreError::IOError(format!(
                "ZoneMetastore.acquire_lock({path}) rejected: {e}"
            ))),
            _ => Err(MetastoreError::IOError(
                "ZoneMetastore.acquire_lock: unexpected result type".into(),
            )),
        }
    }

    fn release_lock(&self, path: &str, lock_id: &str) -> Result<bool, MetastoreError> {
        let cmd = Command::ReleaseLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
        };
        let result = self.runtime.block_on(self.node.propose(cmd)).map_err(|e| {
            MetastoreError::IOError(format!("ZoneMetastore.release_lock({path}): {e}"))
        })?;
        Ok(matches!(result, CommandResult::Success))
    }

    fn extend_lock(
        &self,
        path: &str,
        lock_id: &str,
        ttl_secs: u64,
    ) -> Result<bool, MetastoreError> {
        let cmd = Command::ExtendLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            new_ttl_secs: ttl_secs.min(u32::MAX as u64) as u32,
            now_secs: FullStateMachine::now(),
        };
        let result = self.runtime.block_on(self.node.propose(cmd)).map_err(|e| {
            MetastoreError::IOError(format!("ZoneMetastore.extend_lock({path}): {e}"))
        })?;
        Ok(matches!(result, CommandResult::Success))
    }

    fn get_lock(&self, path: &str) -> Result<Option<KernelLockInfo>, MetastoreError> {
        // F4 C3.6: linearizable read via raft ReadIndex.
        // Matches etcd/Consul/TiKV "reads are linearizable by
        // default" contract. On the hot path `sys_stat` this would
        // be too expensive, so `sys_stat` does not include lock
        // info — callers that want lock state call `lock_get`
        // explicitly and pay the ~0.5ms heartbeat round-trip.
        let key = path.to_string();
        let fut = self
            .node
            .read_linearizable(move |sm: &FullStateMachine| sm.get_lock(&key));
        let lock_opt = self
            .runtime
            .block_on(fut)
            .map_err(|e| {
                MetastoreError::IOError(format!("ZoneMetastore.get_lock({path}) read_index: {e}"))
            })?
            .map_err(|e| {
                MetastoreError::IOError(format!("ZoneMetastore.get_lock({path}): {e:?}"))
            })?;
        Ok(lock_opt.map(raft_lock_to_kernel))
    }

    fn list_locks(
        &self,
        prefix: &str,
        limit: usize,
    ) -> Result<Vec<KernelLockInfo>, MetastoreError> {
        // F4 C3.6: linearizable read via ReadIndex (see get_lock).
        let key = prefix.to_string();
        let fut = self
            .node
            .read_linearizable(move |sm: &FullStateMachine| sm.list_locks(&key, limit));
        let locks = self
            .runtime
            .block_on(fut)
            .map_err(|e| {
                MetastoreError::IOError(format!(
                    "ZoneMetastore.list_locks({prefix}) read_index: {e}"
                ))
            })?
            .map_err(|e| {
                MetastoreError::IOError(format!("ZoneMetastore.list_locks({prefix}): {e:?}"))
            })?;
        Ok(locks.into_iter().map(raft_lock_to_kernel).collect())
    }
}
