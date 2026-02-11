#![allow(clippy::result_large_err)]
//! Sled → redb migration tool.
//!
//! Copies all data from a sled database to a redb database,
//! preserving tree namespaces and key-value pairs.
//!
//! # Usage
//!
//! ```rust,ignore
//! use nexus_raft::storage::migration::migrate_sled_to_redb;
//!
//! let stats = migrate_sled_to_redb("/old/sled/path", "/new/redb/path").unwrap();
//! println!("Migrated {} trees, {} keys", stats.trees_migrated, stats.keys_migrated);
//! ```

use std::path::Path;

use super::redb_store::{RedbStore, StorageError as RedbError};
use super::sled_store::{SledStore, StorageError as SledError};

/// Migration statistics.
#[derive(Debug, Default)]
pub struct MigrationStats {
    /// Number of named trees migrated.
    pub trees_migrated: usize,
    /// Total number of key-value pairs migrated.
    pub keys_migrated: usize,
    /// Number of keys migrated per tree (tree_name → count).
    pub per_tree: Vec<(String, usize)>,
}

/// Errors during migration.
#[derive(Debug, thiserror::Error)]
pub enum MigrationError {
    #[error("sled error: {0}")]
    Sled(#[from] SledError),
    #[error("redb error: {0}")]
    Redb(#[from] RedbError),
    #[error("sled raw error: {0}")]
    SledRaw(#[from] sled::Error),
}

/// Known tree names used by the Nexus Raft system.
const KNOWN_TREES: &[&str] = &[
    "sm_metadata",     // FullStateMachine metadata
    "sm_locks",        // FullStateMachine locks
    "raft_entries",    // RaftStorage log entries
    "raft_state",      // RaftStorage state (hard_state, conf_state, snapshot)
    "witness_log",     // WitnessStateMachine log
    "__sled__default", // sled default tree
];

/// Migrate all data from a sled database to a redb database.
///
/// Opens the sled database at `sled_path`, opens/creates a redb database
/// at `redb_path`, and copies all trees and key-value pairs.
///
/// # Arguments
/// * `sled_path` - Path to the existing sled database directory
/// * `redb_path` - Path for the new redb database file
///
/// # Returns
/// Migration statistics showing what was migrated.
pub fn migrate_sled_to_redb(
    sled_path: impl AsRef<Path>,
    redb_path: impl AsRef<Path>,
) -> Result<MigrationStats, MigrationError> {
    let sled_store = SledStore::open(sled_path)?;
    let redb_store = RedbStore::open(redb_path)?;

    let mut stats = MigrationStats::default();

    // Migrate the default tree (sled's unnamed default tree)
    let default_count = migrate_default_tree(&sled_store, &redb_store)?;
    if default_count > 0 {
        stats
            .per_tree
            .push(("__default__".to_string(), default_count));
        stats.keys_migrated += default_count;
        stats.trees_migrated += 1;
    }

    // Migrate known named trees
    for tree_name in KNOWN_TREES {
        // Skip the sled internal default tree marker
        if *tree_name == "__sled__default" {
            continue;
        }

        let count = migrate_named_tree(&sled_store, &redb_store, tree_name)?;
        if count > 0 {
            stats.per_tree.push((tree_name.to_string(), count));
            stats.keys_migrated += count;
            stats.trees_migrated += 1;
        }
    }

    // Also discover and migrate any trees not in the known list
    let sled_tree_names = sled_store.raw().tree_names();
    for name_bytes in sled_tree_names {
        if let Ok(name) = String::from_utf8(name_bytes.to_vec()) {
            // Skip already-migrated known trees and the sled default tree
            if name == "__sled__default" || KNOWN_TREES.contains(&name.as_str()) {
                continue;
            }
            let count = migrate_named_tree(&sled_store, &redb_store, &name)?;
            if count > 0 {
                stats.per_tree.push((name, count));
                stats.keys_migrated += count;
                stats.trees_migrated += 1;
            }
        }
    }

    Ok(stats)
}

/// Migrate sled's default tree to redb's default table.
fn migrate_default_tree(
    sled_store: &SledStore,
    redb_store: &RedbStore,
) -> Result<usize, MigrationError> {
    let mut count = 0;

    // Iterate all keys in sled's default namespace
    for item in sled_store.raw().iter() {
        let (key, value) = item?;
        redb_store.set(&key, &value)?;
        count += 1;
    }

    Ok(count)
}

/// Migrate a named sled tree to a redb tree.
fn migrate_named_tree(
    sled_store: &SledStore,
    redb_store: &RedbStore,
    tree_name: &str,
) -> Result<usize, MigrationError> {
    // Try to open the sled tree (it might not exist)
    let sled_tree = match sled_store.tree(tree_name) {
        Ok(t) => t,
        Err(_) => return Ok(0), // Tree doesn't exist, skip
    };

    if sled_tree.is_empty() {
        return Ok(0);
    }

    let redb_tree = redb_store.tree(tree_name)?;

    let mut count = 0;
    for item in sled_tree.iter() {
        let (key, value) = item?;
        redb_tree.set(&key, &value)?;
        count += 1;
    }

    Ok(count)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_migrate_empty_database() {
        let sled_dir = tempfile::TempDir::new().unwrap();
        let redb_dir = tempfile::TempDir::new().unwrap();
        let redb_path = redb_dir.path().join("test.redb");

        // Create and immediately close empty sled database
        {
            let _sled = SledStore::open(sled_dir.path()).unwrap();
        }

        let stats = migrate_sled_to_redb(sled_dir.path(), &redb_path).unwrap();

        assert_eq!(stats.trees_migrated, 0);
        assert_eq!(stats.keys_migrated, 0);
    }

    #[test]
    fn test_migrate_default_tree() {
        let sled_dir = tempfile::TempDir::new().unwrap();
        let redb_dir = tempfile::TempDir::new().unwrap();
        let redb_path = redb_dir.path().join("test.redb");

        // Populate sled default tree, then close
        {
            let sled = SledStore::open(sled_dir.path()).unwrap();
            sled.set(b"key1", b"value1").unwrap();
            sled.set(b"key2", b"value2").unwrap();
            sled.flush().unwrap();
        }

        let stats = migrate_sled_to_redb(sled_dir.path(), &redb_path).unwrap();

        assert_eq!(stats.keys_migrated, 2);
        assert_eq!(stats.trees_migrated, 1);

        // Verify data in redb
        let redb = RedbStore::open(&redb_path).unwrap();
        assert_eq!(redb.get(b"key1").unwrap(), Some(b"value1".to_vec()));
        assert_eq!(redb.get(b"key2").unwrap(), Some(b"value2".to_vec()));
    }

    #[test]
    fn test_migrate_named_trees() {
        let sled_dir = tempfile::TempDir::new().unwrap();
        let redb_dir = tempfile::TempDir::new().unwrap();
        let redb_path = redb_dir.path().join("test.redb");

        // Populate sled with named trees, then close
        {
            let sled = SledStore::open(sled_dir.path()).unwrap();
            let metadata = sled.tree("sm_metadata").unwrap();
            metadata.set(b"/file1", b"meta1").unwrap();
            metadata.set(b"/file2", b"meta2").unwrap();
            metadata.set(b"/file3", b"meta3").unwrap();

            let locks = sled.tree("sm_locks").unwrap();
            locks.set(b"/lock1", b"lock_data").unwrap();
            sled.flush().unwrap();
        }

        let stats = migrate_sled_to_redb(sled_dir.path(), &redb_path).unwrap();

        assert_eq!(stats.keys_migrated, 4);
        assert_eq!(stats.trees_migrated, 2);

        // Verify data in redb
        let redb = RedbStore::open(&redb_path).unwrap();
        let meta_tree = redb.tree("sm_metadata").unwrap();
        assert_eq!(meta_tree.get(b"/file1").unwrap(), Some(b"meta1".to_vec()));
        assert_eq!(meta_tree.get(b"/file2").unwrap(), Some(b"meta2".to_vec()));
        assert_eq!(meta_tree.get(b"/file3").unwrap(), Some(b"meta3".to_vec()));

        let lock_tree = redb.tree("sm_locks").unwrap();
        assert_eq!(
            lock_tree.get(b"/lock1").unwrap(),
            Some(b"lock_data".to_vec())
        );
    }

    #[test]
    fn test_migrate_state_machine_roundtrip() {
        use crate::raft::{FullStateMachine, StateMachine};

        let sled_dir = tempfile::TempDir::new().unwrap();
        let redb_dir = tempfile::TempDir::new().unwrap();
        let redb_path = redb_dir.path().join("test.redb");

        // Write metadata directly (as FullStateMachine would), then close sled
        {
            let sled = SledStore::open(sled_dir.path()).unwrap();
            let sled_metadata = sled.tree("sm_metadata").unwrap();
            let sled_locks = sled.tree("sm_locks").unwrap();

            sled_metadata
                .set(b"/test/file.txt", b"metadata_bytes")
                .unwrap();
            sled_metadata
                .set(b"__last_applied__", &5u64.to_be_bytes())
                .unwrap();
            sled_locks
                .set(b"/test/file.txt", b"lock_info_bytes")
                .unwrap();
            sled.flush().unwrap();
        }

        // Migrate sled → redb
        let stats = migrate_sled_to_redb(sled_dir.path(), &redb_path).unwrap();
        assert!(stats.keys_migrated >= 3);

        // Open the migrated redb and create a FullStateMachine on it
        let redb = RedbStore::open(&redb_path).unwrap();
        let sm = FullStateMachine::new(&redb).unwrap();

        // Verify state was preserved
        assert_eq!(sm.last_applied_index(), 5);
        assert_eq!(
            sm.get_metadata("/test/file.txt").unwrap(),
            Some(b"metadata_bytes".to_vec())
        );
    }

    #[test]
    fn test_migrate_unknown_trees() {
        let sled_dir = tempfile::TempDir::new().unwrap();
        let redb_dir = tempfile::TempDir::new().unwrap();
        let redb_path = redb_dir.path().join("test.redb");

        // Create sled with a custom tree name, then close
        {
            let sled = SledStore::open(sled_dir.path()).unwrap();
            let custom = sled.tree("my_custom_tree").unwrap();
            custom.set(b"foo", b"bar").unwrap();
            sled.flush().unwrap();
        }

        let stats = migrate_sled_to_redb(sled_dir.path(), &redb_path).unwrap();

        assert_eq!(stats.keys_migrated, 1);
        assert_eq!(stats.trees_migrated, 1);
        assert_eq!(stats.per_tree[0].0, "my_custom_tree");

        // Verify data in redb
        let redb = RedbStore::open(&redb_path).unwrap();
        let tree = redb.tree("my_custom_tree").unwrap();
        assert_eq!(tree.get(b"foo").unwrap(), Some(b"bar".to_vec()));
    }
}
