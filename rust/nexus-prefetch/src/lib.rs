//! nexus-prefetch — adaptive read-ahead engine.
//!
//! See `docs/superpowers/plans/2026-05-11-issue-4057-nexus-prefetch.md`
//! for architecture. Public surface is `PrefetchEngine` + `RangeReader`.

pub mod config;
pub mod detector;
pub mod engine;
pub mod error;
pub mod metrics;
pub mod pattern;
pub mod range_reader;
pub mod session;
pub mod worker;

#[cfg(feature = "python")]
pub mod pyo3_bindings;

pub use config::EngineConfig;
pub use detector::Detector;
pub use engine::PrefetchEngine;
pub use error::PrefetchError;
pub use pattern::AccessPattern;
pub use range_reader::RangeReader;
pub use session::Session;
