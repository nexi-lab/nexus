// =============================================================================
// Tiger Cache Roaring Bitmap Integration (Issue #896)
// =============================================================================

use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use roaring::RoaringBitmap;

/// Filter path IDs using a pre-materialized Tiger Cache bitmap.
///
/// This provides O(1) permission filtering by using Roaring Bitmap membership
/// checks instead of O(n) ReBAC graph traversal.
///
/// Args:
///     path_int_ids: List of path integer IDs to filter
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     List of path IDs that are present in the bitmap (i.e., accessible)
///
/// Performance:
///     - O(n) where n is the number of path_int_ids
///     - Each membership check is O(1) via bitmap.contains()
///     - Expected 100-1000x speedup vs graph traversal for large lists
#[pyfunction]
pub fn filter_paths_with_tiger_cache(
    path_int_ids: Vec<u32>,
    bitmap_bytes: &[u8],
) -> PyResult<Vec<u32>> {
    // Deserialize the Roaring Bitmap from Python's pyroaring format
    // Both pyroaring and roaring-rs use the standard RoaringFormatSpec
    let bitmap = RoaringBitmap::deserialize_from(bitmap_bytes).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {}",
            e
        ))
    })?;

    // Filter paths using O(1) bitmap membership checks
    let accessible: Vec<u32> = path_int_ids
        .into_iter()
        .filter(|&id| bitmap.contains(id))
        .collect();

    Ok(accessible)
}

/// Filter path IDs using a Tiger Cache bitmap with parallel processing.
///
/// Uses rayon for parallel filtering on large path lists (>1000 paths).
///
/// Args:
///     path_int_ids: List of path integer IDs to filter
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     List of path IDs that are present in the bitmap (i.e., accessible)
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

    // Use parallel iterator for large lists
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
///
/// More efficient than filter when the bitmap is smaller than the path list,
/// as it iterates over the bitmap instead of the path list.
///
/// Args:
///     path_int_ids: Set of path integer IDs to intersect
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     List of path IDs present in both the input set and the bitmap
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

    // Create a bitmap from the input path IDs for set intersection
    let input_bitmap: RoaringBitmap = path_int_ids.into_iter().collect();

    // Perform bitmap intersection (very efficient for Roaring Bitmaps)
    let result = input_bitmap & bitmap;

    Ok(result.iter().collect())
}

/// Check if any path IDs are accessible via Tiger Cache bitmap.
///
/// Fast early-exit check - useful for permission gates.
///
/// Args:
///     path_int_ids: List of path integer IDs to check
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     True if at least one path ID is in the bitmap
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

    // Early exit on first match
    Ok(path_int_ids.iter().any(|&id| bitmap.contains(id)))
}

/// Get statistics about a Tiger Cache bitmap.
///
/// Args:
///     bitmap_bytes: Serialized Roaring Bitmap from Python Tiger Cache
///
/// Returns:
///     Dict with cardinality, serialized_bytes, is_empty
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
