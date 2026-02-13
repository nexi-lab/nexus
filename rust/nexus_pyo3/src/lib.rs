#![allow(clippy::useless_conversion)]

mod bitmap;
mod bloom;
mod cache;
mod glob;
mod hash;
mod io;
mod rebac;
mod search;
mod simd;

use pyo3::prelude::*;

/// Python module definition.
#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    // ReBAC
    m.add_function(wrap_pyfunction!(rebac::compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::expand_subjects, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::list_objects_for_subject, m)?)?;
    // Search
    m.add_function(wrap_pyfunction!(search::grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(search::grep_files_mmap, m)?)?;
    // Glob
    m.add_function(wrap_pyfunction!(glob::glob_match_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(glob::filter_paths, m)?)?;
    // File I/O
    m.add_function(wrap_pyfunction!(io::read_file, m)?)?;
    m.add_function(wrap_pyfunction!(io::read_files_bulk, m)?)?;
    // Tiger Cache Roaring Bitmap
    m.add_function(wrap_pyfunction!(bitmap::filter_paths_with_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(
        bitmap::filter_paths_with_tiger_cache_parallel,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        bitmap::intersect_paths_with_tiger_cache,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        bitmap::any_path_accessible_tiger_cache,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(bitmap::tiger_cache_bitmap_stats, m)?)?;
    // SIMD vector similarity
    m.add_function(wrap_pyfunction!(simd::cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::dot_product_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::euclidean_sq_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::batch_cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::top_k_similar_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(simd::batch_cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(simd::top_k_similar_i8, m)?)?;
    // Hash
    m.add_function(wrap_pyfunction!(hash::hash_content_py, m)?)?;
    m.add_function(wrap_pyfunction!(hash::hash_content_smart_py, m)?)?;
    m.add_function(wrap_pyfunction!(hash::hash_bytes, m)?)?;
    // Classes
    m.add_class::<bloom::BloomFilter>()?;
    m.add_class::<cache::L1MetadataCache>()?;
    Ok(())
}
