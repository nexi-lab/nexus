//! Permission service tier — ReBAC permission provider (§13).
//!
//! [`hook::RebacPermissionHook`] implements the kernel's
//! [`kernel::core::dispatch::PermissionProvider`] trait. Registered via
//! `Kernel::set_permission_provider` at boot; the kernel's permission
//! gate calls it on lease-miss / admin-bypass-miss.

pub mod hook;
