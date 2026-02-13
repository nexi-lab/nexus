#![allow(clippy::useless_conversion)]
// Issue #1396: Split monolithic lib.rs into domain-specific modules
mod blake3_hash;
mod cache;
mod glob_filter;
mod grep;
mod io;
mod rebac;
mod similarity;
mod tiger_cache;

use pyo3::prelude::*;

/// Python module definition
#[pymodule]
fn nexus_fast(m: &Bound<PyModule>) -> PyResult<()> {
    // ReBAC permission engine
    m.add_function(wrap_pyfunction!(rebac::compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::expand_subjects, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::list_objects_for_subject, m)?)?;
    // Content search (grep)
    m.add_function(wrap_pyfunction!(grep::grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(grep::grep_files_mmap, m)?)?;
    // Glob pattern matching
    m.add_function(wrap_pyfunction!(glob_filter::glob_match_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(glob_filter::filter_paths, m)?)?;
    // File I/O
    m.add_function(wrap_pyfunction!(io::read_file, m)?)?;
    m.add_function(wrap_pyfunction!(io::read_files_bulk, m)?)?;
    // Tiger Cache Roaring Bitmap functions (Issue #896)
    m.add_function(wrap_pyfunction!(tiger_cache::filter_paths_with_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(tiger_cache::filter_paths_with_tiger_cache_parallel, m)?)?;
    m.add_function(wrap_pyfunction!(tiger_cache::intersect_paths_with_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(tiger_cache::any_path_accessible_tiger_cache, m)?)?;
    m.add_function(wrap_pyfunction!(tiger_cache::tiger_cache_bitmap_stats, m)?)?;
    // SIMD-accelerated vector similarity functions (Issue #952)
    m.add_function(wrap_pyfunction!(similarity::cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::dot_product_f32, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::euclidean_sq_f32, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::batch_cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::top_k_similar_f32, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::batch_cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(similarity::top_k_similar_i8, m)?)?;
    // BLAKE3 hashing for content-addressable storage (Issue #1395)
    m.add_function(wrap_pyfunction!(blake3_hash::hash_content, m)?)?;
    m.add_function(wrap_pyfunction!(blake3_hash::hash_content_smart, m)?)?;
    // Cache classes
    m.add_class::<cache::BloomFilter>()?;
    m.add_class::<cache::L1MetadataCache>()?;
    Ok(())
}
