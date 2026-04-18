#![allow(clippy::useless_conversion)]

#[cfg(feature = "mimalloc")]
#[global_allocator]
static GLOBAL: mimalloc::MiMalloc = mimalloc::MiMalloc;

/// Canonical root zone identifier — re-exported from the ``contracts``
/// crate (the Rust mirror of ``nexus.contracts.constants``) so kernel
/// users can reach it via ``nexus_kernel::ROOT_ZONE_ID`` without pulling
/// another workspace dep. Prefer this constant over hardcoded ``"root"``
/// literals.
pub use contracts::ROOT_ZONE_ID;

mod agent_registry;
#[cfg(feature = "connectors")]
mod anthropic_backend;
#[cfg(feature = "connectors")]
pub mod anthropic_streaming;
pub mod backend;
mod bitmap;
mod bloom;
mod cas_chunking;
mod cas_engine;
mod cas_remote;
mod cas_transport;
mod dcache;
mod dispatch;
mod dlc;
mod federation_client;
mod file_watch;
#[cfg(feature = "connectors")]
mod gcs_backend;
#[cfg(feature = "connectors")]
mod gdrive_backend;
mod glob;
#[cfg(feature = "connectors")]
mod gmail_backend;
mod hash;
mod hook_registry;
mod io;
mod kernel;
pub mod lock_manager;
mod metadata_debug;
pub mod metastore;
// Mount table (kernel SSOT for mount entries — backend + per-mount
// metastore + access flags). Mirrors Python `nexus.core.mount_table`.
pub mod mount_table;
// `generated_pyo3` kept public so other crates (e.g. `rust/raft`) can
// reference `PyKernel` via cross-crate PyO3 borrows — needed for
// `PyZoneHandle::attach_to_kernel_mount()` which wires a Raft-backed
// `Metastore` into `Kernel::mount_metastores` without surfacing a
// separate `KernelMetastore` Python class.
pub mod generated_pyo3;
#[cfg(feature = "connectors")]
mod openai_backend;
#[cfg(feature = "connectors")]
mod openai_inference;
#[cfg(feature = "connectors")]
pub mod openai_streaming;
mod path_utils;
mod peer_blob_client;
mod permission_hook;
mod pipe;
mod pipe_manager;
mod prefix;
mod raft_metastore;
mod rebac;
mod replication;
#[cfg(feature = "connectors")]
mod s3_backend;
mod search;
mod semaphore;
#[cfg(unix)]
mod shm_pipe;
#[cfg(unix)]
mod shm_stream;
mod simd;
#[cfg(feature = "connectors")]
mod slack_backend;
#[cfg(unix)]
mod stdio_pipe;
mod stdio_stream;
mod stream;
mod stream_manager;
mod stream_observer;
mod trigram;
mod volume_engine;
mod volume_index;
mod wal_stream;

use pyo3::prelude::*;

/// Python module definition.
#[pymodule]
fn nexus_kernel(m: &Bound<PyModule>) -> PyResult<()> {
    // ReBAC
    m.add_function(wrap_pyfunction!(rebac::compute_permissions_bulk, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::compute_permission_single, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::expand_subjects, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::list_objects_for_subject, m)?)?;
    // ReBAC bitmap intersection (§10 C1)
    m.add_function(wrap_pyfunction!(rebac::check_permission_bitmap, m)?)?;
    m.add_function(wrap_pyfunction!(rebac::check_permission_bitmap_batch, m)?)?;
    // OpenAI inference (§10 D3) — GIL-free HTTP calls
    #[cfg(feature = "connectors")]
    {
        m.add_function(wrap_pyfunction!(
            openai_inference::openai_chat_completion,
            m
        )?)?;
        m.add_function(wrap_pyfunction!(
            openai_inference::openai_chat_completion_stream,
            m
        )?)?;
    }
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
    // VFSLockManager deleted — I/O lock is now internal to LockManager,
    // accessed through Kernel syscalls (sys_read/sys_write/sys_copy).
    // MemoryPipeBackend/MemoryStreamBackend are kernel-internal only (no #[pyclass]).
    // Python accesses IPC buffers through kernel.create_pipe/create_stream.
    #[cfg(unix)]
    m.add_class::<shm_pipe::SharedMemoryPipeBackend>()?;
    #[cfg(unix)]
    m.add_class::<shm_stream::SharedMemoryStreamBackend>()?;
    // Raft-backed durable stream (replaces Python `WALStreamBackend`).
    m.add_class::<wal_stream::WalStreamBackend>()?;
    // Subprocess-stdio accumulation stream (Unix raw-fd pump).
    #[cfg(unix)]
    m.add_class::<stdio_stream::StdioStreamBackend>()?;
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

    // FileMetadata proto round-trip (R16.1a) — debug surface for
    // cross-language byte-compat tests. Not a production API.
    m.add_function(wrap_pyfunction!(
        metadata_debug::file_metadata_from_proto_bytes,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(
        metadata_debug::file_metadata_to_proto_bytes,
        m
    )?)?;

    // Federation peer gRPC client (R16.5b).
    m.add_class::<federation_client::PyFederationClient>()?;

    // Register raft's PyO3 classes (ZoneManager, ZoneHandle, …) so
    // Python sees them under ``nexus_kernel`` alongside ``Kernel``.
    nexus_raft::pyo3_bindings::register_python_classes(m)?;

    Ok(())
}
