//! Audit-node bootstrap — concrete-`Kernel` only.
//!
//! Zone creation + learner join reach the federation through
//! `kernel.distributed_coordinator()`, which is a kernel-internal
//! accessor not exposed on the `KernelAbi` trait. Like
//! `audit::install_root`, this path is therefore gated to `K = Kernel`
//! (production wiring); the generic collect loop in the parent module
//! stays `K: KernelAbi`.

use kernel::kernel::Kernel;

use super::AuditNode;

impl AuditNode<Kernel> {
    /// Create the audit-node's own zone and join each production zone as
    /// a raft learner, registering the audit DT_STREAM locally on each.
    ///
    /// Idempotent: `create_zone` treats an existing zone as success;
    /// `join_zone` / `prepare_stream_only` are likewise safe to repeat on
    /// restart. A per-zone failure is logged and skipped so one bad zone
    /// doesn't abort the whole bootstrap.
    pub fn bootstrap(&self, production_zones: &[String]) {
        let coord = self.kernel.distributed_coordinator();

        // 1. Create the audit-node's own central zone.
        match coord.create_zone(self.kernel.as_ref(), &self.audit_zone_id) {
            Ok(()) => {
                tracing::info!(zone = %self.audit_zone_id, "audit-node created audit zone")
            }
            Err(e) => tracing::debug!(
                zone = %self.audit_zone_id,
                error = %e,
                "audit-node audit zone already present (idempotent)"
            ),
        }

        // 2. Join every production zone as a learner + register the audit
        //    stream locally so stream reads see committed records.
        for zone in production_zones {
            if let Err(e) = coord.join_zone(self.kernel.as_ref(), zone, /* as_learner */ true) {
                tracing::warn!(zone = %zone, error = %e, "audit-node join_zone(learner) failed");
                continue;
            }
            if let Err(e) =
                crate::audit::prepare_stream_only(self.kernel.as_ref(), zone, &self.stream_path)
            {
                tracing::warn!(zone = %zone, error = ?e, "audit-node prepare_stream_only failed");
                continue;
            }
            self.resume_zone(zone.clone());
            tracing::info!(zone = %zone, "audit-node joined + registered audit stream");
        }
    }
}
