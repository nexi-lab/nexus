//! `managed-agent-runtime` — cross-repo runtime-body adapters for
//! `services::managed_agent`.
//!
//! Wraps third-party runtime crates as concrete
//! [`services::managed_agent::SpawnTask<Kernel>`] impls. Both the
//! Python wheel cdylib (`nexus-cdylib`) and the cluster binary
//! (`profiles/cluster`) depend on this rlib and reach the
//! managed-agent runtime body through one canonical entry point
//! ([`install_managed_agent_with_sudocode_spawn`]) — no duplicated
//! adapter code at the binary edges.
//!
//! ## Why a separate crate
//!
//! Three constraints made the dedicated crate the cleanest seam for
//! cross-repo coupling (per nexus-para-pc-3 plan PR-C v1, option (b)):
//!
//! 1. **Services rlib must stay runtime-agnostic.** A direct
//!    `sudocode-runtime` git-dep on services would couple the rlib's
//!    build to a specific sudocode rev. Slim deployments (cluster
//!    binary today, future raft-witness, future WASM) link services
//!    without sudocode at all.
//! 2. **Both binary edges need the runtime.** The Python wheel and
//!    the cluster binary both want to ship managed-agent with a real
//!    runtime body. Without a shared crate they would each have to
//!    duplicate the adapter struct + install wiring.
//! 3. **Cross-repo coupling is acceptable in adapter crates.** This
//!    crate's whole purpose is to bridge two repos; it pays the
//!    git-dep cost in a bounded location instead of leaking it into
//!    services or into the binary-edge crates.

use std::sync::Arc;

use kernel::core::agents::registry::AgentDescriptor;
use kernel::kernel::Kernel;
use services::managed_agent::{
    install_managed_agent_with_spawn, SpawnHandle as ServiceSpawnHandle, SpawnTask,
};

// ── Sudo-code adapter ──────────────────────────────────────────────────
//
// Concrete `SpawnTask<Kernel>` impl that wraps
// `sudocode_runtime::spawn_task::spawn_task::<K>(...)`. Generic-K
// monomorphisation: the spawn body inside the adapter compiles
// concretely against the same kernel handle the service holds (no
// per-`sys_read` vtable cost). The DI seam is dyn-dispatched at the
// `SpawnTask::spawn` call boundary — fires once per `start_session`,
// out of the hot path.

struct SudoCodeSpawnHandle(sudocode_runtime::spawn_task::SpawnHandle);

impl ServiceSpawnHandle for SudoCodeSpawnHandle {
    fn abort(&self) {
        // Idempotent — `HookAbortSignal::abort` is `AtomicBool::store(
        // true, Ordering::Release)`; concurrent callers (on_terminate
        // observer + an in-flight `cancel(Session)`) both succeed.
        self.0.abort_signal.abort();
    }
}

struct SudoCodeSpawnAdapter;

impl SpawnTask<Kernel> for SudoCodeSpawnAdapter {
    fn spawn(&self, kernel: Arc<Kernel>, desc: AgentDescriptor) -> Box<dyn ServiceSpawnHandle> {
        let handle = sudocode_runtime::spawn_task::spawn_task::<Kernel>(kernel, desc);
        Box::new(SudoCodeSpawnHandle(handle))
    }
}

/// Install [`services::managed_agent::ManagedAgentService`] on
/// `kernel` with the sudocode-runtime `spawn_task` adapter wired in.
///
/// One canonical entry — both binary edges call this:
///
/// * `nexus-cdylib` from its `nx_managed_agent_install` PyO3 entry
///   (Python wheel boot path, `_wired.py`).
/// * `profiles/cluster` from its main / boot routine before the
///   cluster binary accepts agent traffic.
///
/// Pure-Rust slim deployments without a runtime body (future
/// raft-witness, WASM) skip this and call
/// `services::managed_agent::install_managed_agent` directly, OR
/// land their own `SpawnTask<Kernel>` adapter in this crate when a
/// different runtime is needed.
pub fn install_managed_agent_with_sudocode_spawn(kernel: &Arc<Kernel>) -> Result<(), String> {
    let provider: Arc<dyn SpawnTask<Kernel>> = Arc::new(SudoCodeSpawnAdapter);
    install_managed_agent_with_spawn(kernel, provider)
}
