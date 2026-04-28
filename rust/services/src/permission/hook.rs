//! PermissionHook — Hybrid Rust/Python permission check hook (§11 Phase 11).
//!
//! Architecture:
//!   Fast path: Rust DashMap lease table (~100-200ns)
//!   Slow path: GIL → Python PermissionChecker.check() (~50-200μs)
//!
//! The lease table is a (path, agent_id) → Instant map. On hit, the full
//! ReBAC bitmap check is skipped entirely. On miss, we acquire the GIL,
//! call the Python checker, and stamp a lease on success.
//!
//! This mirrors the Python PermissionCheckHook (permission_hook.py) but
//! moves the hot-path lease check into pure Rust — no GIL for cache hits.
//!
//! Phase 3: moved from `kernel/src/permission_hook.rs` into the services
//! crate.  Currently scaffolding only — `register_native_permission_hook`
//! wiring on PyKernel was never implemented, so this is dead code today.
//! Lives here because conceptually it's a service-tier hook (same tier
//! as `services::audit::AuditHook`) and §11 Phase 11 will wire it up.

use dashmap::DashMap;
use kernel::core::dispatch::{HookContext, HookOutcome, NativeInterceptHook};
use pyo3::prelude::*;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

/// Permission enum matching Python Permission.READ / Permission.WRITE.
#[derive(Debug, Clone, Copy)]
enum Permission {
    Read,
    Write,
}

impl Permission {
    fn as_str(&self) -> &'static str {
        match self {
            Self::Read => "READ",
            Self::Write => "WRITE",
        }
    }
}

/// Lease entry — records when permission was last verified.
struct LeaseEntry {
    granted_at: Instant,
}

/// Rust-native permission check hook with DashMap lease table.
///
/// Implements NativeInterceptHook so it can be registered in the kernel's
/// NativeHookRegistry and dispatched without GIL for lease hits.
#[allow(dead_code)]
pub(crate) struct PermissionHook {
    /// Global toggle — when false, all checks are skipped.
    enforce: AtomicBool,
    /// Python PermissionChecker instance (slow path: GIL required).
    checker: Py<PyAny>,
    /// Python PermissionEnforcer for stat/access (direct check, no raise).
    enforcer: Option<Py<PyAny>>,
    /// Lease table: (path, agent_id) → LeaseEntry.
    leases: DashMap<(String, String), LeaseEntry>,
    /// Lease TTL — leases older than this are evicted on check.
    lease_ttl: Duration,
}

#[allow(dead_code)]
impl PermissionHook {
    /// Create a new permission hook wrapping a Python checker.
    ///
    /// Called from Python during factory boot via PyKernel.register_native_permission_hook().
    pub(crate) fn new(
        checker: Py<PyAny>,
        enforcer: Option<Py<PyAny>>,
        enforce: bool,
        lease_ttl_ms: u64,
    ) -> Self {
        Self {
            enforce: AtomicBool::new(enforce),
            checker,
            enforcer,
            leases: DashMap::new(),
            lease_ttl: Duration::from_millis(lease_ttl_ms),
        }
    }

    /// Check lease — pure Rust, no GIL.
    fn lease_check(&self, path: &str, agent_id: &str) -> bool {
        if agent_id.is_empty() {
            return false;
        }
        let key = (path.to_string(), agent_id.to_string());
        if let Some(entry) = self.leases.get(&key) {
            if entry.granted_at.elapsed() < self.lease_ttl {
                return true;
            }
            // Expired — remove
            drop(entry);
            self.leases.remove(&key);
        }
        false
    }

    /// Stamp lease after successful permission check.
    fn lease_stamp(&self, path: &str, agent_id: &str) {
        if agent_id.is_empty() {
            return;
        }
        self.leases.insert(
            (path.to_string(), agent_id.to_string()),
            LeaseEntry {
                granted_at: Instant::now(),
            },
        );
    }

    /// Slow path: acquire GIL and call Python checker.check(path, permission, context).
    /// Returns Ok(()) on success, Err(message) if permission denied.
    fn python_check(&self, path: &str, perm: Permission) -> Result<(), String> {
        Python::attach(|py| {
            let checker = self.checker.bind(py);
            // Import Permission enum from Python
            let perm_mod = py
                .import("nexus.contracts.types")
                .map_err(|e| format!("import error: {e}"))?;
            let perm_cls = perm_mod
                .getattr("Permission")
                .map_err(|e| format!("Permission not found: {e}"))?;
            let py_perm = perm_cls
                .getattr(perm.as_str())
                .map_err(|e| format!("Permission.{} not found: {e}", perm.as_str()))?;

            match checker.call_method1("check", (path, py_perm)) {
                Ok(_) => Ok(()),
                Err(e) => {
                    // PermissionError from Python → deny
                    Err(format!("Permission denied: {e}"))
                }
            }
        })
    }

    /// Check permission with lease fast-path.
    fn check_with_lease(&self, path: &str, agent_id: &str, perm: Permission) -> Result<(), String> {
        // Fast path: lease hit (~100-200ns)
        if self.lease_check(path, agent_id) {
            return Ok(());
        }
        // Slow path: Python checker (~50-200μs)
        self.python_check(path, perm)?;
        // Stamp lease on success
        self.lease_stamp(path, agent_id);
        Ok(())
    }

    /// Stat/access permission check via enforcer (returns bool, doesn't raise).
    fn enforcer_check(&self, path: &str, perm_str: &str) -> Result<(), String> {
        let enforcer = match &self.enforcer {
            Some(e) => e,
            None => return Ok(()), // No enforcer — allow
        };
        Python::attach(|py| {
            let enf = enforcer.bind(py);
            let perm_mod = py
                .import("nexus.contracts.types")
                .map_err(|e| format!("import error: {e}"))?;
            let perm_cls = perm_mod
                .getattr("Permission")
                .map_err(|e| format!("Permission not found: {e}"))?;
            let py_perm = perm_cls
                .getattr(perm_str)
                .map_err(|e| format!("Permission.{perm_str} not found: {e}"))?;

            let result: bool = enf
                .call_method1("check", (path, py_perm))
                .and_then(|r| r.extract())
                .unwrap_or(false);

            if result {
                Ok(())
            } else {
                Err(format!(
                    "Access denied: no {perm_str} permission for '{path}'"
                ))
            }
        })
    }
}

impl NativeInterceptHook for PermissionHook {
    fn name(&self) -> &str {
        "permission_check"
    }

    fn on_pre(&self, ctx: &HookContext) -> Result<HookOutcome, String> {
        if !self.enforce.load(Ordering::Relaxed) {
            return Ok(HookOutcome::Pass);
        }

        let identity = ctx.identity();

        // Admin bypass
        if identity.is_admin {
            return Ok(HookOutcome::Pass);
        }

        let check: Result<(), String> = match ctx {
            HookContext::Read(c) => {
                self.check_with_lease(&c.path, &identity.agent_id, Permission::Read)
            }
            HookContext::Write(c) => {
                self.check_with_lease(&c.path, &identity.agent_id, Permission::Write)
            }
            HookContext::Delete(c) => {
                self.check_with_lease(&c.path, &identity.agent_id, Permission::Write)
            }
            HookContext::Rename(c) => {
                // Check WRITE on both paths
                self.python_check(&c.old_path, Permission::Write)
                    .and_then(|()| self.python_check(&c.new_path, Permission::Write))
            }
            HookContext::Copy(c) => {
                // READ on source, WRITE on destination
                self.python_check(&c.src_path, Permission::Read)
                    .and_then(|()| self.python_check(&c.dst_path, Permission::Write))
            }
            HookContext::Mkdir(c) => self.python_check(&c.path, Permission::Write),
            HookContext::Rmdir(c) => {
                self.check_with_lease(&c.path, &identity.agent_id, Permission::Write)
            }
            HookContext::Stat(c) => self.enforcer_check(&c.path, &c.permission),
            HookContext::Access(c) => self.enforcer_check(&c.path, &c.permission),
            HookContext::WriteBatch(_) => Ok(()), // Batch checks individual paths
        };
        check.map(|()| HookOutcome::Pass)
    }
}
