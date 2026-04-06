#![allow(clippy::useless_conversion)]

#[cfg(feature = "mimalloc")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

mod backend;
mod bitmap;
mod bloom;
mod cas_engine;
mod cas_transport;
mod dcache;
mod dispatch;
mod generated_pyo3;
mod glob;
mod grpc_backend;
mod hash;
mod hook_registry;
mod io;
mod kernel;
mod lock;
mod metastore;
mod path_utils;
mod pipe;
mod prefix;
mod rebac;
mod router;
mod search;
mod semaphore;
#[cfg(unix)]
mod shm_pipe;
#[cfg(unix)]
mod shm_stream;
mod simd;
mod stream;
mod trigram;
mod volume_engine;
mod volume_index;

use pyo3::prelude::*;

/// Python module definition.
#[pymodule]
fn nexus_kernel(m: &Bound<PyModule>) -> PyResult<()> {
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
    // Path prefix matching (Issue #1565)
    m.add_function(wrap_pyfunction!(prefix::any_path_starts_with, m)?)?;
    m.add_function(wrap_pyfunction!(prefix::batch_prefix_check, m)?)?;
    m.add_function(wrap_pyfunction!(prefix::filter_paths_by_prefix, m)?)?;
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
    // Classes
    m.add_class::<bloom::BloomFilter>()?;
    m.add_class::<lock::VFSLockManager>()?;
    m.add_class::<pipe::RingBufferCore>()?;
    m.add_class::<stream::StreamBufferCore>()?;
    #[cfg(unix)]
    m.add_class::<shm_pipe::SharedRingBufferCore>()?;
    #[cfg(unix)]
    m.add_class::<shm_stream::SharedStreamBufferCore>()?;
    m.add_class::<semaphore::VFSSemaphore>()?;
    // CAS Volume Engine (Issue #3403)
    m.add_class::<volume_engine::VolumeEngine>()?;
    // Route result (returned from Kernel.route()) — now PyRustRouteResult
    m.add_class::<generated_pyo3::PyRustRouteResult>()?;
    // Kernel (Issue #1868 — PyKernel wraps pure Rust Kernel)
    m.add_class::<generated_pyo3::PyOperationContext>()?;
    m.add_class::<generated_pyo3::PyKernel>()?;
    m.add_class::<generated_pyo3::PySysReadResult>()?;
    m.add_class::<generated_pyo3::PySysWriteResult>()?;
    // Path utilities (Issue #1817 prerequisite)
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
    Ok(())
}
