//! Durable task queue engine — service-tier impl.
//!
//! Sub-modules:
//!   * [`engine`] — `Engine` struct (open, submit, claim, complete, …)
//!   * [`store`] — fjall-backed persistence
//!   * [`task`] — `TaskRecord`, `TaskStatus`, `TaskPriority` types
//!   * [`priority`] — priority queue ordering
//!   * [`retry`] — retry / back-off policy
//!   * [`error`] — `TaskError` enum

pub mod engine;
pub mod error;
pub mod priority;
pub mod retry;
pub mod store;
pub mod task;
