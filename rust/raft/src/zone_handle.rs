//! Pure-Rust `ZoneHandle` — per-zone raft node handle.
//!
//! Extracted from `PyZoneHandle` (pyo3_bindings.rs) in R20.18.1 so the
//! kernel crate can use zone handles without going through the PyO3
//! boundary. `PyZoneHandle` in pyo3_bindings.rs is now a thin wrapper
//! that holds `Arc<ZoneHandle>` and converts errors to `PyErr`.
//!
//! Per v20.10 boundary rule: `ZoneHandle` is kernel-internal and MUST
//! NOT be exposed to Python directly — only syscalls / dispatch hooks /
//! 4-pillar storage traits cross the PyO3 boundary. The `PyZoneHandle`
//! wrapper is transitional — R20.18.6 deletes it after Python callers
//! are cut over to syscalls in R20.18.5.

#![cfg(all(feature = "grpc", has_protos))]

use std::sync::Arc;

use crate::raft::{
    Command, CommandResult, FullStateMachine, LockAcquireResult, LockInfo, LockMode, RaftError,
    Result, ZoneConsensus,
};
// Bring the `StateMachine` trait into scope so the closures below can
// call methods like `get_metadata` / `list_metadata` through the trait.
#[allow(unused_imports)]
use crate::raft::StateMachine;

/// Consistency mode for replicated writes.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Consistency {
    /// Strong consistency — wait for raft commit.
    Sc,
    /// Eventual consistency — local write + WAL token.
    Ec,
}

/// Handle to a single zone's raft node (pure Rust, kernel-internal).
pub struct ZoneHandle {
    node: ZoneConsensus<FullStateMachine>,
    runtime_handle: tokio::runtime::Handle,
    zone_id: String,
}

impl ZoneHandle {
    pub(crate) fn new(
        node: ZoneConsensus<FullStateMachine>,
        runtime_handle: tokio::runtime::Handle,
        zone_id: String,
    ) -> Arc<Self> {
        Arc::new(Self {
            node,
            runtime_handle,
            zone_id,
        })
    }

    pub fn zone_id(&self) -> &str {
        &self.zone_id
    }

    pub fn consensus_node(&self) -> ZoneConsensus<FullStateMachine> {
        self.node.clone()
    }

    pub fn runtime_handle(&self) -> tokio::runtime::Handle {
        self.runtime_handle.clone()
    }

    pub fn is_leader(&self) -> bool {
        self.node.is_leader()
    }

    pub fn leader_id(&self) -> Option<u64> {
        self.node.leader_id()
    }

    pub fn commit_index(&self) -> u64 {
        self.node.commit_index()
    }

    pub fn term(&self) -> u64 {
        self.node.term()
    }

    pub fn applied_index(&self) -> u64 {
        self.node.applied_index()
    }

    pub fn is_committed(&self, token: u64) -> Option<String> {
        self.node.is_committed(token).map(|s| s.to_string())
    }

    // ── Metadata operations ────────────────────────────────────────

    pub fn set_metadata(
        &self,
        path: &str,
        value: Vec<u8>,
        consistency: Consistency,
    ) -> Result<Option<u64>> {
        let cmd = Command::SetMetadata {
            key: path.to_string(),
            value,
        };
        match consistency {
            Consistency::Ec => Ok(Some(self.propose_ec_local(cmd)?)),
            Consistency::Sc => {
                self.propose(cmd)?;
                Ok(None)
            }
        }
    }

    pub fn cas_set_metadata(
        &self,
        path: &str,
        value: Vec<u8>,
        expected_version: u32,
        _consistency: Consistency,
    ) -> Result<(bool, u32)> {
        let cmd = Command::CasSetMetadata {
            key: path.to_string(),
            value,
            expected_version,
        };
        match self.propose_raw(cmd)? {
            CommandResult::CasResult {
                success,
                current_version,
            } => Ok((success, current_version)),
            _ => Err(RaftError::InvalidState(
                "Unexpected CAS result type".to_string(),
            )),
        }
    }

    pub fn adjust_counter(&self, key: &str, delta: i64) -> Result<i64> {
        let cmd = Command::AdjustCounter {
            key: key.to_string(),
            delta,
        };
        match self.propose_raw(cmd)? {
            CommandResult::Value(bytes) => {
                let arr: [u8; 8] = bytes
                    .try_into()
                    .map_err(|_| RaftError::InvalidState("Invalid counter value".to_string()))?;
                Ok(i64::from_be_bytes(arr))
            }
            _ => Err(RaftError::InvalidState(
                "Unexpected adjust_counter result type".to_string(),
            )),
        }
    }

    pub fn get_metadata(&self, path: &str) -> Result<Option<Vec<u8>>> {
        let node = self.node.clone();
        let path = path.to_string();
        self.runtime_handle.block_on(async move {
            node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata(&path))
                .await
        })
    }

    pub fn delete_metadata(&self, path: &str, consistency: Consistency) -> Result<Option<u64>> {
        let cmd = Command::DeleteMetadata {
            key: path.to_string(),
        };
        match consistency {
            Consistency::Ec => Ok(Some(self.propose_ec_local(cmd)?)),
            Consistency::Sc => {
                self.propose(cmd)?;
                Ok(None)
            }
        }
    }

    pub fn list_metadata(&self, prefix: &str) -> Result<Vec<(String, Vec<u8>)>> {
        let node = self.node.clone();
        let prefix = prefix.to_string();
        self.runtime_handle.block_on(async move {
            node.with_state_machine(|sm: &FullStateMachine| sm.list_metadata(&prefix))
                .await
        })
    }

    pub fn get_metadata_multi(&self, paths: Vec<String>) -> Result<Vec<(String, Option<Vec<u8>>)>> {
        let node = self.node.clone();
        self.runtime_handle.block_on(async move {
            node.with_state_machine(|sm: &FullStateMachine| sm.get_metadata_multi(&paths))
                .await
        })
    }

    pub fn batch_set_metadata(&self, items: Vec<(String, Vec<u8>)>) -> Result<usize> {
        let count = items.len();
        for (path, value) in items {
            self.propose(Command::SetMetadata { key: path, value })?;
        }
        Ok(count)
    }

    pub fn batch_delete_metadata(&self, keys: Vec<String>) -> Result<usize> {
        let count = keys.len();
        for key in keys {
            self.propose(Command::DeleteMetadata { key })?;
        }
        Ok(count)
    }

    // ── Lock operations (always SC) ────────────────────────────────

    #[allow(clippy::too_many_arguments)]
    pub fn acquire_lock(
        &self,
        path: &str,
        lock_id: &str,
        max_holders: u32,
        ttl_secs: u32,
        holder_info: &str,
        mode: LockMode,
    ) -> Result<LockAcquireResult> {
        let cmd = Command::AcquireLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            max_holders,
            ttl_secs,
            holder_info: holder_info.to_string(),
            mode,
            now_secs: FullStateMachine::now(),
        };
        match self.propose_raw(cmd)? {
            CommandResult::LockResult(state) => Ok(state),
            _ => Err(RaftError::InvalidState(
                "Unexpected acquire_lock result type".to_string(),
            )),
        }
    }

    pub fn release_lock(&self, path: &str, lock_id: &str) -> Result<bool> {
        let cmd = Command::ReleaseLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
        };
        Ok(matches!(self.propose_raw(cmd)?, CommandResult::Success))
    }

    pub fn extend_lock(&self, path: &str, lock_id: &str, new_ttl_secs: u32) -> Result<bool> {
        let cmd = Command::ExtendLock {
            path: path.to_string(),
            lock_id: lock_id.to_string(),
            new_ttl_secs,
            now_secs: FullStateMachine::now(),
        };
        Ok(matches!(self.propose_raw(cmd)?, CommandResult::Success))
    }

    pub fn get_lock(&self, path: &str) -> Result<Option<LockInfo>> {
        let node = self.node.clone();
        let path = path.to_string();
        self.runtime_handle.block_on(async move {
            node.with_state_machine(|sm: &FullStateMachine| sm.get_lock(&path))
                .await
        })
    }

    pub fn list_locks(&self, prefix: &str, limit: usize) -> Result<Vec<LockInfo>> {
        let node = self.node.clone();
        let prefix = prefix.to_string();
        self.runtime_handle.block_on(async move {
            node.with_state_machine(|sm: &FullStateMachine| sm.list_locks(&prefix, limit))
                .await
        })
    }

    // ── Internal propose helpers ───────────────────────────────────

    fn propose_ec_local(&self, cmd: Command) -> Result<u64> {
        let node = self.node.clone();
        self.runtime_handle
            .block_on(async move { node.propose_ec_local(cmd).await })
    }

    fn propose(&self, cmd: Command) -> Result<bool> {
        match self.propose_raw(cmd)? {
            CommandResult::Success => Ok(true),
            CommandResult::Error(e) => Err(RaftError::Raft(e)),
            CommandResult::LockResult(state) => Ok(state.acquired),
            CommandResult::CasResult { success, .. } => Ok(success),
            CommandResult::Value(_) => Ok(true),
        }
    }

    fn propose_raw(&self, cmd: Command) -> Result<CommandResult> {
        let node = self.node.clone();
        self.runtime_handle
            .block_on(async move { node.propose(cmd).await })
    }
}
