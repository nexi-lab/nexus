//! Syscall credential carried through every kernel operation.
//!
//! Lifted out of `rust/kernel/src/kernel.rs` so out-of-kernel
//! services (`rust/services/src/{acp,managed_agent,…}/`) can build
//! `OperationContext` values for system-tier calls without pulling
//! kernel as a dep just for this struct. Kernel re-exports under
//! the historical `kernel::kernel::OperationContext` path so every
//! existing call site keeps compiling.
//!
//! Analogous to Linux `struct cred` — immutable after construction.
//! Constructed by thin wrappers (Python, gRPC, in-process service
//! calls) with identity fields. The kernel uses `zone_id` for
//! routing; hooks use the full context.

#[derive(Clone, Debug)]
pub struct OperationContext {
    /// Subject identity (human user or service account).
    pub user_id: String,
    /// Routing zone — NexusFS instance zone for mount lookup
    /// (always set).
    pub zone_id: String,
    /// Admin privilege flag.
    pub is_admin: bool,
    /// Agent identity (optional, for agent-initiated operations).
    pub agent_id: Option<String>,
    /// System operation flag (bypasses all checks).
    pub is_system: bool,
    /// Group memberships for ReBAC.
    pub groups: Vec<String>,
    /// Granted admin capabilities (e.g. "MANAGE_ZONES",
    /// "READ_ALL").
    pub admin_capabilities: Vec<String>,
    /// Subject type for ReBAC (default: "user").
    pub subject_type: String,
    /// Subject ID for ReBAC (defaults to user_id).
    pub subject_id: Option<String>,
    /// Audit trail correlation ID.
    pub request_id: String,
    /// Caller's zone_id (None = no zone restriction). Distinct
    /// from routing zone_id.
    pub context_zone_id: Option<String>,
}

impl OperationContext {
    pub fn new(
        user_id: &str,
        zone_id: &str,
        is_admin: bool,
        agent_id: Option<&str>,
        is_system: bool,
    ) -> Self {
        Self {
            user_id: user_id.to_string(),
            zone_id: zone_id.to_string(),
            is_admin,
            agent_id: agent_id.map(|s| s.to_string()),
            is_system,
            groups: Vec::new(),
            admin_capabilities: Vec::new(),
            subject_type: "user".to_string(),
            subject_id: None,
            request_id: String::new(),
            context_zone_id: None,
        }
    }
}
