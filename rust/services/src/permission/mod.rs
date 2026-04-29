//! Permission service tier — kernel-side permission-check hooks.
//!
//! Currently scaffolding: the [`hook::PermissionHook`] type implements
//! [`kernel::core::dispatch::NativeInterceptHook`] and would be
//! registered via `Kernel::register_native_hook` — wiring is tracked
//! under §11.  Lives here for the architectural classification (hook
//! impls go to services tier, not kernel).

pub mod hook;
