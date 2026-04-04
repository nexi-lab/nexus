//! Tiger Cache Roaring Bitmap integration (PyO3 wrappers).

use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use roaring::RoaringBitmap;

/// Filter path IDs using a pre-materialized Tiger Cache bitmap.
#[pyfunction]
pub fn filter_paths_with_tiger_cache(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let accessible = py.detach(|| {
        path_int_ids
            .into_iter()
            .filter(|&id| bitmap.contains(id))
            .collect::<Vec<u32>>()
    });

    Ok(accessible)
}

/// Filter path IDs using a Tiger Cache bitmap with parallel processing.
#[pyfunction]
pub fn filter_paths_with_tiger_cache_parallel(
    py: Python<'_>,
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

    let accessible = py.detach(|| {
        if path_int_ids.len() > PARALLEL_THRESHOLD {
            path_int_ids
                .into_par_iter()
                .filter(|&id| bitmap.contains(id))
                .collect::<Vec<u32>>()
        } else {
            path_int_ids
                .into_iter()
                .filter(|&id| bitmap.contains(id))
                .collect::<Vec<u32>>()
        }
    });

    Ok(accessible)
}

/// Compute the intersection of path IDs with a Tiger Cache bitmap.
#[pyfunction]
pub fn intersect_paths_with_tiger_cache(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let result = py.detach(|| {
        let input_bitmap: RoaringBitmap = path_int_ids.into_iter().collect();
        let intersection = input_bitmap & bitmap;
        intersection.iter().collect::<Vec<u32>>()
    });

    Ok(result)
}

/// Check if any path IDs are accessible via Tiger Cache bitmap.
#[pyfunction]
pub fn any_path_accessible_tiger_cache(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<bool> {
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let result = py.detach(|| path_int_ids.iter().any(|&id| bitmap.contains(id)));

    Ok(result)
}

/// Get statistics about a Tiger Cache bitmap.
#[pyfunction]
pub fn tiger_cache_bitmap_stats(py: Python<'_>, bitmap_bytes: &[u8]) -> PyResult<Py<PyAny>> {
    let serialized_len = bitmap_bytes.len();
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    let (cardinality, is_empty) = py.detach(|| (bitmap.len(), bitmap.is_empty()));

    let dict = PyDict::new(py);
    dict.set_item("cardinality", cardinality)?;
    dict.set_item("serialized_bytes", serialized_len)?;
    dict.set_item("is_empty", is_empty)?;
    Ok(dict.into())
}
