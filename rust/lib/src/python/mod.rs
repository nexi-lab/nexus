//! `lib::python` — PyO3 wrappers around the pure-Rust algorithms in
//! `crate::*` siblings (path / prefix / simd / io / search / trigram /
//! glob / rebac). Compiled only when the `python` feature is on
//! (kernel cdylib is the sole consumer today).
//!
//! The kernel cdylib's `#[pymodule]` calls a single delegation point —
//! `lib::python::register(m)` — instead of registering each
//! function/class itself. Kernels / wasm builds that don't need PyO3
//! disable the `python` feature and never link pyo3.

use pyo3::prelude::*;

pub mod bitmap;
pub mod bloom;
pub mod glob;
pub mod hash;
pub mod io;
pub mod path_utils;
pub mod prefix;
pub mod rebac;
pub mod search;
pub mod simd;
pub mod trigram;

/// Register every `lib::python::*` PyO3 export into the parent module.
/// Called from `kernel/src/lib.rs`'s `#[pymodule] fn nexus_runtime`.
pub fn register(m: &Bound<PyModule>) -> PyResult<()> {
    // ReBAC
    m.add_function(wrap_pyfunction!(rebac::compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::expand_subjects, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::list_objects_for_subject, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::check_permission_bitmap, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::check_permission_bitmap_batch, m)?)?;
    // Search
    m.add_function(wrap_pyfunction!(search::grep_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(search::grep_files_mmap, m)?)?;
    // Glob
    m.add_function(wrap_pyfunction!(glob::glob_match_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(glob::filter_paths, m)?)?;
    // File I/O
    m.add_function(wrap_pyfunction!(io::read_file, m)?)?;
    m.add_function(wrap_pyfunction!(io::read_files_bulk, m)?)?;
    // Path prefix matching
    m.add_function(wrap_pyfunction!(prefix::any_path_starts_with, m)?)?;
    m.add_function(wrap_pyfunction!(prefix::batch_prefix_check, m)?)?;
    m.add_function(wrap_pyfunction!(prefix::filter_paths_by_prefix, m)?)?;
    // SIMD
    m.add_function(wrap_pyfunction!(simd::cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::dot_product_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::euclidean_sq_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::batch_cosine_similarity_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::top_k_similar_f32, m)?)?;
    m.add_function(wrap_pyfunction!(simd::cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(simd::batch_cosine_similarity_i8, m)?)?;
    m.add_function(wrap_pyfunction!(simd::top_k_similar_i8, m)?)?;
    // Trigram Index
    m.add_function(wrap_pyfunction!(trigram::build_trigram_index, m)?)?;
    m.add_function(wrap_pyfunction!(
        trigram::build_trigram_index_from_entries,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(trigram::trigram_grep, m)?)?;
    m.add_function(wrap_pyfunction!(trigram::trigram_search_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(trigram::trigram_index_stats, m)?)?;
    m.add_function(wrap_pyfunction!(trigram::invalidate_trigram_cache, m)?)?;
    // Path utilities
    m.add_function(wrap_pyfunction!(path_utils::split_path, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::get_parent, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::get_ancestors, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::get_parent_chain, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::parent_path, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::validate_path, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::normalize_path, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::path_matches_pattern, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::unscope_internal_path, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::canonicalize_path, m)?)?;
    m.add_function(wrap_pyfunction!(path_utils::extract_zone_id, m)?)?;
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
    // Hash (lib::hash::* algorithms back the wrappers)
    m.add_function(wrap_pyfunction!(hash::hash_content_py, m)?)?;
    m.add_function(wrap_pyfunction!(hash::hash_content_smart_py, m)?)?;
    m.add_function(wrap_pyfunction!(hash::hash_bytes, m)?)?;
    // BloomFilter pyclass (the lib `Bloom` pure-Rust impl is a
    // separate WASM-clean module, not surfaced here).
    m.add_class::<bloom::BloomFilter>()?;
    Ok(())
}
