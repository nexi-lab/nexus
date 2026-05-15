//! RebacPermissionHook — implements PermissionProvider (§13).
//!
//! Architecture:
//!   The kernel permission gate (Kernel::check_permission) calls this
//!   provider on lease-miss / admin-bypass-miss. Single GIL crossing
//!   to the Python PermissionChecker (was 3-4 before §13).
//!
//! Hot path (lease hit, admin bypass, system path): zero GIL crossings.
//! Cold path (lease miss → this provider): 1 GIL crossing + SQL.
//!
//! Lives in services tier (same tier as `services::audit::AuditHook`).
//! Registered via `Kernel::set_permission_provider` at boot.

use kernel::core::dispatch::{Permission, PermissionDecision, PermissionProvider};
use pyo3::prelude::*;
use std::sync::atomic::{AtomicBool, Ordering};

/// Rust-side ReBAC permission provider wrapping a Python checker.
///
/// Implements `PermissionProvider` so it can be registered with the
/// kernel's permission gate via `Kernel::set_permission_provider`.
#[allow(dead_code)]
pub struct RebacPermissionHook {
    /// Python PermissionChecker instance (slow path: GIL required).
    checker: Py<PyAny>,
    /// Global toggle — when false, returns Unknown (kernel allows).
    enforce: AtomicBool,
}

#[allow(dead_code)]
impl RebacPermissionHook {
    /// Create a new ReBAC permission hook wrapping a Python checker.
    pub fn new(checker: Py<PyAny>, enforce: bool) -> Self {
        Self {
            checker,
            enforce: AtomicBool::new(enforce),
        }
    }

    /// Toggle enforcement at runtime.
    pub fn set_enforce(&self, enforce: bool) {
        self.enforce.store(enforce, Ordering::Relaxed);
    }
}

impl PermissionProvider for RebacPermissionHook {
    fn check(
        &self,
        path: &str,
        permission: Permission,
        _ctx: &contracts::OperationContext,
    ) -> PermissionDecision {
        if !self.enforce.load(Ordering::Relaxed) {
            return PermissionDecision::Unknown;
        }

        // Single GIL crossing (was 4 before §13)
        Python::attach(|py| {
            let checker = self.checker.bind(py);

            // Import Permission enum from Python
            let perm_mod = match py.import("nexus.contracts.types") {
                Ok(m) => m,
                Err(_) => return PermissionDecision::Unknown,
            };
            let perm_cls = match perm_mod.getattr("Permission") {
                Ok(c) => c,
                Err(_) => return PermissionDecision::Unknown,
            };
            let py_perm = match perm_cls.getattr(permission.as_str()) {
                Ok(p) => p,
                Err(_) => return PermissionDecision::Unknown,
            };

            match checker.call_method1("check", (path, py_perm)) {
                Ok(_) => PermissionDecision::Allow,
                Err(_) => PermissionDecision::Deny(format!(
                    "permission denied: {} on '{}'",
                    permission.as_str(),
                    path
                )),
            }
        })
    }
}
