//! Tiger Cache Roaring Bitmap integration (PyO3 wrappers).

use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;
use roaring::RoaringBitmap;

/// Copy the Python-owned bytes into an owned `Vec<u8>` so the subsequent
/// deserialization + filter can run without holding the GIL
/// (§ review fix #16). Also centralizes the error message.
#[inline]
fn deserialize_bitmap(bitmap_bytes: Vec<u8>) -> Result<RoaringBitmap, pyo3::PyErr> {
    RoaringBitmap::deserialize_from(bitmap_bytes.as_slice()).map_err(|e| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "Failed to deserialize Tiger Cache bitmap: {e}"
        ))
    })
}

/// Filter path IDs using a pre-materialized Tiger Cache bitmap.
#[pyfunction]
pub fn filter_paths_with_tiger_cache(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: Vec<u8>,
) -> PyResult<Vec<u32>> {
    let (err, accessible) = py.detach(|| match deserialize_bitmap(bitmap_bytes) {
        Ok(bitmap) => (
            None,
            path_int_ids
                .into_iter()
                .filter(|&id| bitmap.contains(id))
                .collect::<Vec<u32>>(),
        ),
        Err(e) => (Some(e), Vec::new()),
    });
    if let Some(e) = err {
        return Err(e);
    }
    Ok(accessible)
}

/// Filter path IDs using a Tiger Cache bitmap with parallel processing.
#[pyfunction]
pub fn filter_paths_with_tiger_cache_parallel(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: Vec<u8>,
) -> PyResult<Vec<u32>> {
    const PARALLEL_THRESHOLD: usize = 1000;

    let (err, accessible) = py.detach(|| match deserialize_bitmap(bitmap_bytes) {
        Ok(bitmap) => {
            let out = if path_int_ids.len() > PARALLEL_THRESHOLD {
                path_int_ids
                    .into_par_iter()
                    .filter(|&id| bitmap.contains(id))
                    .collect::<Vec<u32>>()
            } else {
                path_int_ids
                    .into_iter()
                    .filter(|&id| bitmap.contains(id))
                    .collect::<Vec<u32>>()
            };
            (None, out)
        }
        Err(e) => (Some(e), Vec::new()),
    });
    if let Some(e) = err {
        return Err(e);
    }
    Ok(accessible)
}

/// Compute the intersection of path IDs with a Tiger Cache bitmap.
#[pyfunction]
pub fn intersect_paths_with_tiger_cache(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: Vec<u8>,
) -> PyResult<Vec<u32>> {
    let (err, result) = py.detach(|| match deserialize_bitmap(bitmap_bytes) {
        Ok(bitmap) => {
            let input_bitmap: RoaringBitmap = path_int_ids.into_iter().collect();
            let intersection = input_bitmap & bitmap;
            (None, intersection.iter().collect::<Vec<u32>>())
        }
        Err(e) => (Some(e), Vec::new()),
    });
    if let Some(e) = err {
        return Err(e);
    }
    Ok(result)
}

/// Check if any path IDs are accessible via Tiger Cache bitmap.
#[pyfunction]
pub fn any_path_accessible_tiger_cache(
    py: Python<'_>,
    path_int_ids: Vec<u32>,
    bitmap_bytes: Vec<u8>,
) -> PyResult<bool> {
    let (err, result) = py.detach(|| match deserialize_bitmap(bitmap_bytes) {
        Ok(bitmap) => (None, path_int_ids.iter().any(|&id| bitmap.contains(id))),
        Err(e) => (Some(e), false),
    });
    if let Some(e) = err {
        return Err(e);
    }
    Ok(result)
}

/// Get statistics about a Tiger Cache bitmap.
#[pyfunction]
pub fn tiger_cache_bitmap_stats(py: Python<'_>, bitmap_bytes: Vec<u8>) -> PyResult<Py<PyAny>> {
    let serialized_len = bitmap_bytes.len();
    let (err, card_empty) = py.detach(|| match deserialize_bitmap(bitmap_bytes) {
        Ok(bitmap) => (None, (bitmap.len(), bitmap.is_empty())),
        Err(e) => (Some(e), (0, true)),
    });
    if let Some(e) = err {
        return Err(e);
    }
    let (cardinality, is_empty) = card_empty;

    let dict = PyDict::new(py);
    dict.set_item("cardinality", cardinality)?;
    dict.set_item("serialized_bytes", serialized_len)?;
    dict.set_item("is_empty", is_empty)?;
    Ok(dict.into())
}
