//! Tiger Cache Roaring Bitmap integration (PyO3 wrappers).

use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use roaring::RoaringBitmap;

/// Filter path IDs using a pre-materialized Tiger Cache bitmap.
#[pyfunction]
pub fn filter_paths_with_tiger_cache(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let accessible: Vec<u32> = path_int_ids
        .into_iter()
        .filter(|&id| bitmap.contains(id))
        .collect();

    Ok(accessible)
}

/// Filter path IDs using a Tiger Cache bitmap with parallel processing.
#[pyfunction]
pub fn filter_paths_with_tiger_cache_parallel(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    const PARALLEL_THRESHOLD: usize = 1000;

    let accessible: Vec<u32> = if path_int_ids.len() > PARALLEL_THRESHOLD {
        path_int_ids
            .into_par_iter()
            .filter(|&id| bitmap.contains(id))
            .collect()
    } else {
        path_int_ids
            .into_iter()
            .filter(|&id| bitmap.contains(id))
            .collect()
    };

    Ok(accessible)
}

/// Compute the intersection of path IDs with a Tiger Cache bitmap.
#[pyfunction]
pub fn intersect_paths_with_tiger_cache(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let input_bitmap: RoaringBitmap = path_int_ids.into_iter().collect();
    let result = input_bitmap & bitmap;

    Ok(result.iter().collect())
}

/// Check if any path IDs are accessible via Tiger Cache bitmap.
#[pyfunction]
pub fn any_path_accessible_tiger_cache(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<bool> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    Ok(path_int_ids.iter().any(|&id| bitmap.contains(id)))
}

/// Get statistics about a Tiger Cache bitmap.
#[pyfunction]
pub fn tiger_cache_bitmap_stats(py: Python<'_>, bitmap_bytes: &[u8]) -> PyResult<Py<PyAny>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let dict = PyDict::new(py);
    dict.set_item("cardinality", bitmap.len())?;
    dict.set_item("serialized_bytes", bitmap_bytes.len())?;
    dict.set_item("is_empty", bitmap.is_empty())?;
    Ok(dict.into())
}
