//! redb-backed storage for password_vault.
//!
//! Two tables:
//!   - `entries`: key = title (utf-8 str), value = bincode(EntryIndex).
//!     One row per title; tracks current_version + tombstone state.
//!   - `versions`: key = byte-encoded (title, version), value =
//!     bincode(StoredEntry). One row per (title, version); holds the
//!     encrypted body + plaintext metadata.
//!
//! Composite key encoding for `versions`: `title.as_bytes() || 0 ||
//! version.to_be_bytes()`. Sorts naturally — all rows for one title
//! cluster together, ordered by version. Matches the byte-key
//! convention used elsewhere in the workspace (kernel meta_store +
//! raft state_machine all use `&[u8]` keys with manual encoding;
//! tuple keys are not the established pattern).
//!
//! All operations are short ACID transactions — redb provides WAL-style
//! durability natively.

use std::path::Path;

use redb::{Database, ReadableDatabase, ReadableTable, TableDefinition};

use super::types::{EntryIndex, PasswordVaultError, StoredEntry};

const ENTRIES: TableDefinition<&str, &[u8]> = TableDefinition::new("entries");
const VERSIONS: TableDefinition<&[u8], &[u8]> = TableDefinition::new("versions");

/// Encode `(title, version)` as a byte key for the `versions` table.
/// `title.as_bytes() || 0 || u32_be(version)` sorts so that all rows
/// for one title cluster together, ordered by ascending version.
fn version_key(title: &str, version: u32) -> Vec<u8> {
    let mut out = Vec::with_capacity(title.len() + 1 + 4);
    out.extend_from_slice(title.as_bytes());
    out.push(0u8);
    out.extend_from_slice(&version.to_be_bytes());
    out
}

/// Range bounds for "all versions of title `t`" — `t || 0 || 0u32..t
/// || 0 || u32::MAX`. Inclusive on both ends (we use `..=` in `range`).
fn version_range(title: &str) -> (Vec<u8>, Vec<u8>) {
    (version_key(title, 0), version_key(title, u32::MAX))
}

pub(crate) struct Storage {
    db: Database,
}

impl Storage {
    /// Open (or create) the vault redb at `path`. Initialises both
    /// tables up-front so the schema is always present, even on
    /// empty-vault startup.
    pub(crate) fn open(path: &Path) -> Result<Self, PasswordVaultError> {
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                std::fs::create_dir_all(parent).map_err(|e| {
                    PasswordVaultError::Storage(format!(
                        "mkdir parent of {}: {e}",
                        path.display()
                    ))
                })?;
            }
        }
        let db = Database::create(path).map_err(|e| {
            PasswordVaultError::Storage(format!("open redb at {}: {e}", path.display()))
        })?;
        // Force table creation up-front.
        let tx = db
            .begin_write()
            .map_err(|e| PasswordVaultError::Storage(format!("begin init tx: {e}")))?;
        {
            let _ = tx
                .open_table(ENTRIES)
                .map_err(|e| PasswordVaultError::Storage(format!("init ENTRIES: {e}")))?;
            let _ = tx
                .open_table(VERSIONS)
                .map_err(|e| PasswordVaultError::Storage(format!("init VERSIONS: {e}")))?;
        }
        tx.commit()
            .map_err(|e| PasswordVaultError::Storage(format!("commit init: {e}")))?;
        Ok(Self { db })
    }

    pub(crate) fn get_index(
        &self,
        title: &str,
    ) -> Result<Option<EntryIndex>, PasswordVaultError> {
        let tx = self
            .db
            .begin_read()
            .map_err(|e| PasswordVaultError::Storage(format!("begin read: {e}")))?;
        let table = tx
            .open_table(ENTRIES)
            .map_err(|e| PasswordVaultError::Storage(format!("open ENTRIES: {e}")))?;
        let row = table
            .get(title)
            .map_err(|e| PasswordVaultError::Storage(format!("get index {title}: {e}")))?;
        match row {
            Some(v) => {
                let idx: EntryIndex = bincode::deserialize(v.value()).map_err(|e| {
                    PasswordVaultError::Storage(format!("decode index {title}: {e}"))
                })?;
                Ok(Some(idx))
            }
            None => Ok(None),
        }
    }

    pub(crate) fn list_indexes(
        &self,
    ) -> Result<Vec<(String, EntryIndex)>, PasswordVaultError> {
        let tx = self
            .db
            .begin_read()
            .map_err(|e| PasswordVaultError::Storage(format!("begin read: {e}")))?;
        let table = tx
            .open_table(ENTRIES)
            .map_err(|e| PasswordVaultError::Storage(format!("open ENTRIES: {e}")))?;
        let iter = table
            .iter()
            .map_err(|e| PasswordVaultError::Storage(format!("iter ENTRIES: {e}")))?;
        let mut out = Vec::new();
        for entry in iter {
            let (k, v) = entry
                .map_err(|e| PasswordVaultError::Storage(format!("entry iter: {e}")))?;
            let title = k.value().to_string();
            let idx: EntryIndex = bincode::deserialize(v.value()).map_err(|e| {
                PasswordVaultError::Storage(format!("decode index {title}: {e}"))
            })?;
            out.push((title, idx));
        }
        Ok(out)
    }

    pub(crate) fn set_index(
        &self,
        title: &str,
        idx: &EntryIndex,
    ) -> Result<(), PasswordVaultError> {
        let encoded = bincode::serialize(idx).map_err(|e| {
            PasswordVaultError::Storage(format!("encode index {title}: {e}"))
        })?;
        let tx = self
            .db
            .begin_write()
            .map_err(|e| PasswordVaultError::Storage(format!("begin write: {e}")))?;
        {
            let mut table = tx
                .open_table(ENTRIES)
                .map_err(|e| PasswordVaultError::Storage(format!("open ENTRIES: {e}")))?;
            table
                .insert(title, encoded.as_slice())
                .map_err(|e| PasswordVaultError::Storage(format!("insert index {title}: {e}")))?;
        }
        tx.commit().map_err(|e| {
            PasswordVaultError::Storage(format!("commit set_index {title}: {e}"))
        })?;
        Ok(())
    }

    pub(crate) fn put_version(
        &self,
        title: &str,
        version: u32,
        entry: &StoredEntry,
    ) -> Result<(), PasswordVaultError> {
        let encoded = bincode::serialize(entry)
            .map_err(|e| PasswordVaultError::Storage(format!("encode version: {e}")))?;
        let key = version_key(title, version);
        let tx = self
            .db
            .begin_write()
            .map_err(|e| PasswordVaultError::Storage(format!("begin write: {e}")))?;
        {
            let mut table = tx
                .open_table(VERSIONS)
                .map_err(|e| PasswordVaultError::Storage(format!("open VERSIONS: {e}")))?;
            table
                .insert(key.as_slice(), encoded.as_slice())
                .map_err(|e| {
                    PasswordVaultError::Storage(format!(
                        "insert version {title}/{version}: {e}"
                    ))
                })?;
        }
        tx.commit().map_err(|e| {
            PasswordVaultError::Storage(format!("commit put_version {title}/{version}: {e}"))
        })?;
        Ok(())
    }

    pub(crate) fn get_version(
        &self,
        title: &str,
        version: u32,
    ) -> Result<Option<StoredEntry>, PasswordVaultError> {
        let key = version_key(title, version);
        let tx = self
            .db
            .begin_read()
            .map_err(|e| PasswordVaultError::Storage(format!("begin read: {e}")))?;
        let table = tx
            .open_table(VERSIONS)
            .map_err(|e| PasswordVaultError::Storage(format!("open VERSIONS: {e}")))?;
        let row = table.get(key.as_slice()).map_err(|e| {
            PasswordVaultError::Storage(format!("get version {title}/{version}: {e}"))
        })?;
        match row {
            Some(v) => {
                let entry: StoredEntry = bincode::deserialize(v.value()).map_err(|e| {
                    PasswordVaultError::Storage(format!(
                        "decode version {title}/{version}: {e}"
                    ))
                })?;
                Ok(Some(entry))
            }
            None => Ok(None),
        }
    }

    /// Iterate all versions of a single title, sorted by ascending
    /// version number (natural key order from the byte encoding).
    pub(crate) fn list_versions(
        &self,
        title: &str,
    ) -> Result<Vec<StoredEntry>, PasswordVaultError> {
        let (lo, hi) = version_range(title);
        let tx = self
            .db
            .begin_read()
            .map_err(|e| PasswordVaultError::Storage(format!("begin read: {e}")))?;
        let table = tx
            .open_table(VERSIONS)
            .map_err(|e| PasswordVaultError::Storage(format!("open VERSIONS: {e}")))?;
        let iter = table
            .range(lo.as_slice()..=hi.as_slice())
            .map_err(|e| {
                PasswordVaultError::Storage(format!("range VERSIONS {title}: {e}"))
            })?;
        let mut out = Vec::new();
        for entry in iter {
            let (_, v) = entry
                .map_err(|e| PasswordVaultError::Storage(format!("version iter: {e}")))?;
            let stored: StoredEntry = bincode::deserialize(v.value()).map_err(|e| {
                PasswordVaultError::Storage(format!("decode version row: {e}"))
            })?;
            out.push(stored);
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn fresh() -> (TempDir, Storage) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("vault.redb");
        let s = Storage::open(&path).unwrap();
        (dir, s)
    }

    fn entry(version: u32, ct: &[u8]) -> StoredEntry {
        StoredEntry {
            version,
            created_at_ms: 1_000,
            nonce: [0u8; 12],
            ciphertext: ct.to_vec(),
        }
    }
    fn index(version: u32, deleted: bool) -> EntryIndex {
        EntryIndex {
            current_version: version,
            deleted_at_ms: if deleted { Some(2_000) } else { None },
        }
    }

    #[test]
    fn empty_db_returns_none() {
        let (_d, s) = fresh();
        assert!(s.get_index("nope").unwrap().is_none());
        assert!(s.get_version("nope", 1).unwrap().is_none());
        assert!(s.list_indexes().unwrap().is_empty());
        assert!(s.list_versions("nope").unwrap().is_empty());
    }

    #[test]
    fn index_round_trip() {
        let (_d, s) = fresh();
        s.set_index("gmail", &index(3, false)).unwrap();
        let got = s.get_index("gmail").unwrap().unwrap();
        assert_eq!(got.current_version, 3);
        assert!(got.deleted_at_ms.is_none());
    }

    #[test]
    fn version_round_trip() {
        let (_d, s) = fresh();
        s.put_version("gmail", 1, &entry(1, &[1, 2, 3])).unwrap();
        let got = s.get_version("gmail", 1).unwrap().unwrap();
        assert_eq!(got.version, 1);
        assert_eq!(got.ciphertext, vec![1, 2, 3]);
    }

    #[test]
    fn list_versions_orders_by_version_and_filters_by_title() {
        let (_d, s) = fresh();
        s.put_version("gmail", 1, &entry(1, b"a")).unwrap();
        s.put_version("gmail", 3, &entry(3, b"c")).unwrap();
        s.put_version("gmail", 2, &entry(2, b"b")).unwrap();
        // unrelated title — must not appear in gmail's history
        s.put_version("github", 1, &entry(1, b"x")).unwrap();

        let versions = s.list_versions("gmail").unwrap();
        let vers: Vec<u32> = versions.iter().map(|e| e.version).collect();
        assert_eq!(vers, vec![1, 2, 3]);
    }

    #[test]
    fn list_indexes_multi() {
        let (_d, s) = fresh();
        s.set_index("gmail", &index(1, false)).unwrap();
        s.set_index("github", &index(2, true)).unwrap();
        let mut idxs = s.list_indexes().unwrap();
        idxs.sort_by(|a, b| a.0.cmp(&b.0));
        assert_eq!(idxs.len(), 2);
        assert_eq!(idxs[0].0, "github");
        assert!(idxs[0].1.deleted_at_ms.is_some());
        assert_eq!(idxs[1].0, "gmail");
        assert!(idxs[1].1.deleted_at_ms.is_none());
    }

    #[test]
    fn persists_across_open() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("v.redb");
        {
            let s = Storage::open(&path).unwrap();
            s.set_index("k", &index(5, false)).unwrap();
            s.put_version("k", 5, &entry(5, b"data")).unwrap();
        }
        // Reopen — data survives.
        let s = Storage::open(&path).unwrap();
        let idx = s.get_index("k").unwrap().unwrap();
        assert_eq!(idx.current_version, 5);
        let v = s.get_version("k", 5).unwrap().unwrap();
        assert_eq!(v.version, 5);
        assert_eq!(v.ciphertext, b"data");
    }

    #[test]
    fn version_key_encoding_sorts_naturally() {
        // Sanity check the encoding: same title, ascending versions
        // produce ascending bytes; different titles produce different
        // prefixes that don't interleave.
        let a1 = version_key("a", 1);
        let a2 = version_key("a", 2);
        let b1 = version_key("b", 1);
        assert!(a1 < a2);
        assert!(a2 < b1);
    }
}
