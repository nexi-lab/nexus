use crate::client::{FileMetadata, NexusClient};
use crate::passthrough::backing::{BackingStore, MaterializedBacking};
use crate::passthrough::config::PassthroughConfig;
use crate::passthrough::policy::{DenyReason, OpenAccess, PassthroughDecision, PassthroughPolicy};
use anyhow::{anyhow, Result};
use fuser::BackingId;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, MutexGuard};

const FIRST_FILE_HANDLE: u64 = 1;
const EXHAUSTED_FILE_HANDLE: u64 = 0;

pub struct ActivePassthrough {
    pub backing: MaterializedBacking,
    pub backing_id: Arc<BackingId>,
}

enum ActiveHandle {
    Passthrough(ActivePassthrough),
    #[cfg(test)]
    Test,
}

pub struct PassthroughManager {
    server_url: String,
    policy: PassthroughPolicy,
    store: BackingStore,
    next_fh: AtomicU64,
    negotiated: AtomicBool,
    active: Mutex<HashMap<u64, ActiveHandle>>,
}

impl PassthroughManager {
    pub fn new(server_url: String, config: PassthroughConfig) -> Result<Self> {
        let root = config
            .backing_dir
            .clone()
            .unwrap_or_else(default_backing_dir);
        Ok(Self {
            server_url,
            policy: PassthroughPolicy::new(config)?,
            store: BackingStore::new(root)?,
            next_fh: AtomicU64::new(FIRST_FILE_HANDLE),
            negotiated: AtomicBool::new(false),
            active: Mutex::new(HashMap::new()),
        })
    }

    pub fn next_file_handle(&self) -> Result<u64> {
        let mut current = self.next_fh.load(Ordering::Relaxed);
        loop {
            if current == EXHAUSTED_FILE_HANDLE {
                return Err(anyhow!("passthrough file handle space exhausted"));
            }

            let next = current.checked_add(1).unwrap_or(EXHAUSTED_FILE_HANDLE);
            match self.next_fh.compare_exchange_weak(
                current,
                next,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => return Ok(current),
                Err(actual) => current = actual,
            }
        }
    }

    pub fn set_negotiated(&self, negotiated: bool) {
        self.negotiated.store(negotiated, Ordering::Release);
    }

    pub fn negotiated(&self) -> bool {
        self.negotiated.load(Ordering::Acquire)
    }

    pub fn require(&self) -> bool {
        self.policy.config().require
    }

    pub fn decide(
        &self,
        path: &str,
        metadata: &FileMetadata,
        access: OpenAccess,
    ) -> PassthroughDecision {
        if !self.negotiated() {
            return PassthroughDecision::Deny(DenyReason::NotNegotiated);
        }
        self.policy.decide(path, metadata, access)
    }

    pub fn materialize(
        &self,
        path: &str,
        client: &NexusClient,
        metadata: &FileMetadata,
    ) -> Result<MaterializedBacking> {
        self.store
            .materialize(&self.server_url, path, client, metadata)
    }

    pub fn insert_active(&self, fh: u64, active: ActivePassthrough) -> Result<()> {
        self.insert_active_handle(fh, ActiveHandle::Passthrough(active))
    }

    pub fn remove_active(&self, fh: u64) -> Result<Option<ActivePassthrough>> {
        match self.active_lock()?.remove(&fh) {
            Some(ActiveHandle::Passthrough(active)) => Ok(Some(active)),
            #[cfg(test)]
            Some(ActiveHandle::Test) => Ok(None),
            None => Ok(None),
        }
    }

    pub fn invalidate_path(&self, path: &str) {
        if let Err(err) = self.store.invalidate_path(&self.server_url, path) {
            log::warn!(
                "passthrough backing invalidation failed for {}: {}",
                path,
                err
            );
        }
    }

    fn insert_active_handle(&self, fh: u64, active: ActiveHandle) -> Result<()> {
        if fh == EXHAUSTED_FILE_HANDLE {
            return Err(anyhow!("passthrough file handle 0 is reserved"));
        }

        let mut active_handles = self.active_lock()?;
        if active_handles.contains_key(&fh) {
            return Err(anyhow!("passthrough file handle {} is already active", fh));
        }
        active_handles.insert(fh, active);
        Ok(())
    }

    fn active_lock(&self) -> Result<MutexGuard<'_, HashMap<u64, ActiveHandle>>> {
        self.active
            .lock()
            .map_err(|_| anyhow!("passthrough active handle map lock poisoned"))
    }

    #[cfg(test)]
    fn set_next_file_handle_for_tests(&self, next_fh: u64) {
        self.next_fh.store(next_fh, Ordering::Relaxed);
    }

    #[cfg(test)]
    fn insert_test_active(&self, fh: u64) -> Result<()> {
        self.insert_active_handle(fh, ActiveHandle::Test)
    }

    #[cfg(test)]
    fn remove_test_active(&self, fh: u64) -> Result<bool> {
        Ok(self.active_lock()?.remove(&fh).is_some())
    }

    #[cfg(test)]
    fn contains_active_for_tests(&self, fh: u64) -> Result<bool> {
        Ok(self.active_lock()?.contains_key(&fh))
    }
}

fn default_backing_dir() -> PathBuf {
    dirs::cache_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join("nexus-fuse")
        .join("passthrough")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::FileMetadata;
    use crate::passthrough::config::PassthroughConfig;
    use tempfile::TempDir;

    struct ManagerFixture {
        manager: PassthroughManager,
        _backing_dir: TempDir,
    }

    impl ManagerFixture {
        fn new(mut config: PassthroughConfig) -> Self {
            let backing_dir = tempfile::tempdir().expect("tempdir");
            config.backing_dir = Some(backing_dir.path().to_path_buf());
            let manager =
                PassthroughManager::new("http://server".to_string(), config).expect("manager");
            Self {
                manager,
                _backing_dir: backing_dir,
            }
        }
    }

    fn metadata(size: u64) -> FileMetadata {
        FileMetadata {
            size,
            gen: 1,
            etag: Some("etag-1".to_string()),
            modified_at: None,
            is_directory: false,
        }
    }

    #[test]
    fn next_file_handle_is_monotonic_and_never_zero() {
        let fixture = ManagerFixture::new(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec![],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        let manager = &fixture.manager;

        assert_eq!(manager.next_file_handle().unwrap(), 1);
        assert_eq!(manager.next_file_handle().unwrap(), 2);
    }

    #[test]
    fn next_file_handle_reports_exhaustion_without_returning_zero() {
        let fixture = ManagerFixture::new(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec![],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        let manager = &fixture.manager;

        manager.set_next_file_handle_for_tests(u64::MAX - 1);

        assert_eq!(manager.next_file_handle().unwrap(), u64::MAX - 1);
        assert_eq!(manager.next_file_handle().unwrap(), u64::MAX);
        assert!(manager.next_file_handle().is_err());
        assert!(manager.next_file_handle().is_err());
    }

    #[test]
    fn decision_uses_policy() {
        let fixture = ManagerFixture::new(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec!["/data/**".to_string()],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        let manager = &fixture.manager;
        manager.set_negotiated(true);

        assert_eq!(
            manager.decide("/data/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Allow
        );
        assert_eq!(
            manager.decide("/logs/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::Pattern)
        );
    }

    #[test]
    fn decision_denies_when_not_negotiated_by_default() {
        let fixture = ManagerFixture::new(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec!["/data/**".to_string()],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        let manager = &fixture.manager;

        assert_eq!(
            manager.decide("/data/big.bin", &metadata(1024), OpenAccess::ReadOnly),
            PassthroughDecision::Deny(DenyReason::NotNegotiated)
        );
    }

    #[test]
    fn require_reflects_config() {
        let fixture = ManagerFixture::new(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec![],
            deny_patterns: vec![],
            require: true,
            backing_dir: None,
        });

        assert!(fixture.manager.require());
    }

    #[test]
    fn active_map_tracks_inserted_handles() {
        let fixture = ManagerFixture::new(PassthroughConfig {
            enabled: true,
            threshold_bytes: 128,
            allow_patterns: vec![],
            deny_patterns: vec![],
            require: false,
            backing_dir: None,
        });
        let manager = &fixture.manager;

        manager.insert_test_active(10).unwrap();

        assert!(manager.contains_active_for_tests(10).unwrap());
        assert!(manager.remove_test_active(10).unwrap());
        assert!(!manager.contains_active_for_tests(10).unwrap());
        assert!(!manager.remove_test_active(10).unwrap());
    }
}
