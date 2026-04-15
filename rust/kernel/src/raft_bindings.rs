//! Kernel-side Python bindings that bridge raft's ``ZoneHandle`` into the
//! kernel's per-mount ``Metastore`` map.
//!
//! F2 C8 (Option A): both ``PyKernel`` and ``PyZoneHandle`` live inside
//! the single ``nexus_kernel`` cdylib now, so this function can build a
//! ``ZoneMetastore`` (a Rust-native ``kernel::Metastore`` impl backed by
//! ``ZoneConsensus``) and install it on ``Kernel::mount_metastores``
//! directly — no cross-cdylib FFI, no ``thread_local!`` duplication.

use pyo3::prelude::*;

use nexus_raft::pyo3_bindings::PyZoneHandle;

use crate::generated_pyo3::PyKernel;
use crate::kernel::Kernel;
use crate::raft_metastore::ZoneMetastore;

/// Install a per-mount ``ZoneMetastore`` backed by a Raft ``ZoneHandle``
/// onto the kernel's mount map.
///
/// Call from Python ``DriverLifecycleCoordinator.mount()`` for
/// federation zones, right after ``kernel.add_mount(...)`` has
/// registered the mount entry. The canonical key is computed the
/// same way the kernel computes it internally, so ``with_metastore``
/// (and thus every ``sys_*`` cold-dcache path) will find it.
#[pyfunction]
pub fn attach_raft_zone_to_kernel(
    kernel: &Bound<'_, PyKernel>,
    zone_handle: &Bound<'_, PyZoneHandle>,
    mount_point: &str,
    zone_id: &str,
) -> PyResult<()> {
    let handle = zone_handle.borrow();
    let node = handle.consensus_node();
    let runtime = handle.runtime_handle();
    let ms = ZoneMetastore::new_arc(node, runtime);
    let canonical = Kernel::canonical_mount_key(mount_point, zone_id);
    kernel.borrow().install_mount_metastore(canonical, ms);
    Ok(())
}
