//! L1 Metadata Cache — lock-free in-memory cache for connector content metadata.

use dashmap::DashMap;
use memmap2::Mmap;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::fs::File;
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// Metadata for a cached file entry.
#[derive(Clone, Debug)]
struct CacheMetadata {
    path_id: String,
    content_hash: String,
    disk_path: PathBuf,
    original_size: u64,
    synced_at: u64,
    ttl_seconds: u32,
    is_text: bool,
    #[allow(dead_code)]
    zone_id: String,
}

/// L1 Metadata Cache — lock-free in-memory cache for connector content metadata.
#[pyclass]
pub struct L1MetadataCache {
    cache: DashMap<String, CacheMetadata>,
    max_entries: usize,
    default_ttl: u32,
    hits: AtomicU64,
    misses: AtomicU64,
}

#[pymethods]
impl L1MetadataCache {
    #[new]
    #[pyo3(signature = (max_entries=100000, default_ttl=300))]
    fn new(max_entries: usize, default_ttl: u32) -> Self {
        Self {
            cache: DashMap::with_capacity(max_entries),
            max_entries,
            default_ttl,
            hits: AtomicU64::new(0),
            misses: AtomicU64::new(0),
        }
    }

    #[pyo3(signature = (key, path_id, content_hash, disk_path, original_size, ttl_seconds=0, is_text=true, zone_id="default"))]
    #[allow(clippy::too_many_arguments)]
    fn put(
        &self,
        key: &str,
        path_id: &str,
        content_hash: &str,
        disk_path: &str,
        original_size: u64,
        ttl_seconds: i32,
        is_text: bool,
        zone_id: &str,
    ) {
        if self.cache.len() >= self.max_entries {
            let to_remove = self.max_entries / 10;
            let keys_to_remove: Vec<String> = self
                .cache
                .iter()
                .take(to_remove)
                .map(|entry| entry.key().clone())
                .collect();
            for k in keys_to_remove {
                self.cache.remove(&k);
            }
        }

        let ttl = match ttl_seconds.cmp(&0) {
            std::cmp::Ordering::Equal => self.default_ttl,
            std::cmp::Ordering::Less => 0,
            std::cmp::Ordering::Greater => ttl_seconds as u32,
        };

        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        let metadata = CacheMetadata {
            path_id: path_id.to_string(),
            content_hash: content_hash.to_string(),
            disk_path: PathBuf::from(disk_path),
            original_size,
            synced_at: now,
            ttl_seconds: ttl,
            is_text,
            zone_id: zone_id.to_string(),
        };

        self.cache.insert(key.to_string(), metadata);
    }

    fn get(&self, key: &str) -> Option<(String, String, String, u64, bool, bool)> {
        match self.cache.get(key) {
            Some(entry) => {
                self.hits.fetch_add(1, Ordering::Relaxed);

                let metadata = entry.value();
                let is_fresh = if metadata.ttl_seconds == 0 {
                    true
                } else {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    let age = now.saturating_sub(metadata.synced_at);
                    age < metadata.ttl_seconds as u64
                };

                Some((
                    metadata.path_id.clone(),
                    metadata.content_hash.clone(),
                    metadata.disk_path.to_string_lossy().to_string(),
                    metadata.original_size,
                    metadata.is_text,
                    is_fresh,
                ))
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                None
            }
        }
    }

    fn get_content(
        &self,
        py: Python<'_>,
        key: &str,
    ) -> PyResult<Option<(Py<PyBytes>, String, bool)>> {
        let metadata = match self.cache.get(key) {
            Some(entry) => {
                let m = entry.value();
                if m.ttl_seconds > 0 {
                    let now = SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    let age = now.saturating_sub(m.synced_at);
                    if age >= m.ttl_seconds as u64 {
                        self.misses.fetch_add(1, Ordering::Relaxed);
                        return Ok(None);
                    }
                }
                self.hits.fetch_add(1, Ordering::Relaxed);
                (m.disk_path.clone(), m.content_hash.clone(), m.is_text)
            }
            None => {
                self.misses.fetch_add(1, Ordering::Relaxed);
                return Ok(None);
            }
        };

        let (disk_path, content_hash, is_text) = metadata;

        let file = match File::open(&disk_path) {
            Ok(f) => f,
            Err(_) => return Ok(None),
        };

        let file_metadata = match file.metadata() {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };

        if file_metadata.len() == 0 {
            return Ok(Some((PyBytes::new(py, &[]).into(), content_hash, is_text)));
        }

        let mmap = match unsafe { Mmap::map(&file) } {
            Ok(m) => m,
            Err(_) => return Ok(None),
        };

        Ok(Some((
            PyBytes::new(py, &mmap).into(),
            content_hash,
            is_text,
        )))
    }

    fn remove(&self, key: &str) -> bool {
        self.cache.remove(key).is_some()
    }

    fn remove_prefix(&self, prefix: &str) -> usize {
        let keys_to_remove: Vec<String> = self
            .cache
            .iter()
            .filter(|entry| entry.key().starts_with(prefix))
            .map(|entry| entry.key().clone())
            .collect();

        let count = keys_to_remove.len();
        for key in keys_to_remove {
            self.cache.remove(&key);
        }
        count
    }

    fn clear(&self) {
        self.cache.clear();
        self.hits.store(0, Ordering::Relaxed);
        self.misses.store(0, Ordering::Relaxed);
    }

    fn stats(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        let hits = self.hits.load(Ordering::Relaxed);
        let misses = self.misses.load(Ordering::Relaxed);
        let total = hits + misses;
        let hit_rate = if total > 0 {
            hits as f64 / total as f64
        } else {
            0.0
        };

        let dict = PyDict::new(py);
        dict.set_item("entries", self.cache.len())?;
        dict.set_item("hits", hits)?;
        dict.set_item("misses", misses)?;
        dict.set_item("hit_rate", hit_rate)?;
        dict.set_item("max_entries", self.max_entries)?;
        dict.set_item("default_ttl", self.default_ttl)?;
        Ok(dict.into())
    }

    #[getter]
    fn len(&self) -> usize {
        self.cache.len()
    }

    fn is_empty(&self) -> bool {
        self.cache.is_empty()
    }

    #[getter]
    fn memory_bytes(&self) -> usize {
        self.cache.len() * 150
    }
}
