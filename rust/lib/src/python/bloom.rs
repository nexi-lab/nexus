//! Bloom filter for fast cache miss detection (PyO3 wrapper).

use bloomfilter::Bloom;
use pyo3::prelude::*;
use std::sync::RwLock;

/// Bloom filter for fast cache miss detection.
///
/// §review fix #17: store the filter over `str` (`Bloom<str>`) so `set`/
/// `check` accept a borrowed `&str` directly; previously we allocated a
/// `String` per call to match the `Bloom<String>` generic.
#[pyclass]
pub struct BloomFilter {
    bloom: RwLock<Bloom<str>>,
    capacity: usize,
    fp_rate: f64,
}

/// Helper to convert a poisoned RwLock error into a Python RuntimeError.
fn lock_err<T>(e: std::sync::PoisonError<T>) -> PyErr {
    pyo3::exceptions::PyRuntimeError::new_err(format!("BloomFilter lock poisoned: {}", e))
}

#[pymethods]
impl BloomFilter {
    #[new]
    #[pyo3(signature = (expected_items=100000, fp_rate=0.01))]
    fn new(expected_items: usize, fp_rate: f64) -> PyResult<Self> {
        let bloom = Bloom::new_for_fp_rate(expected_items, fp_rate).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to create Bloom filter: {}", e))
        })?;
        Ok(Self {
            bloom: RwLock::new(bloom),
            capacity: expected_items,
            fp_rate,
        })
    }

    fn add(&self, key: &str) -> PyResult<()> {
        self.bloom.write().map_err(lock_err)?.set(key);
        Ok(())
    }

    fn add_bulk(&self, py: Python<'_>, keys: Vec<String>) -> PyResult<()> {
        // Take the lock *inside* `py.detach` — std's RwLockWriteGuard is not
        // Send, so the closure must acquire and release within the same
        // thread. Holding the GIL across bloom hashing of a large batch is
        // what we want to avoid here.
        py.detach(|| -> PyResult<()> {
            let mut bloom = self.bloom.write().map_err(lock_err)?;
            for key in &keys {
                bloom.set(key.as_str());
            }
            Ok(())
        })
    }

    fn might_exist(&self, key: &str) -> PyResult<bool> {
        Ok(self.bloom.read().map_err(lock_err)?.check(key))
    }

    fn check_bulk(&self, py: Python<'_>, keys: Vec<String>) -> PyResult<Vec<bool>> {
        py.detach(|| -> PyResult<Vec<bool>> {
            let bloom = self.bloom.read().map_err(lock_err)?;
            Ok(keys.iter().map(|k| bloom.check(k.as_str())).collect())
        })
    }

    fn clear(&self) -> PyResult<()> {
        let new_bloom = Bloom::new_for_fp_rate(self.capacity, self.fp_rate).map_err(|e| {
            pyo3::exceptions::PyValueError::new_err(format!("Failed to clear Bloom filter: {}", e))
        })?;
        *self.bloom.write().map_err(lock_err)? = new_bloom;
        Ok(())
    }

    #[getter]
    fn capacity(&self) -> usize {
        self.capacity
    }

    #[getter]
    fn fp_rate(&self) -> f64 {
        self.fp_rate
    }

    #[getter]
    fn memory_bytes(&self) -> usize {
        let bits_per_item = (-1.44 * (self.fp_rate).ln() / (2.0_f64).ln()) as usize;
        (self.capacity * bits_per_item).div_ceil(8)
    }
}
