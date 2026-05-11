//! Pluggable pattern detector trait.  Implementations:
//! - `SequentialDetector` (forward-progressing reads)
//! - `StrideDetector` (fixed-delta strided access)
//! - `MajorityTrendDetector` (Leap ATC'20)

use crate::pattern::AccessPattern;

pub mod sequential;
pub mod stride;
pub mod trend;

pub use sequential::SequentialDetector;
pub use stride::StrideDetector;
pub use trend::MajorityTrendDetector;

/// Detector contract — observe (offset, size) tuples in arrival order
/// and emit an `AccessPattern` recommendation.  Detectors are
/// per-file-handle, not shared.
pub trait Detector: Send + 'static {
    /// Feed one observation; receive a pattern hint.
    fn observe(&mut self, offset: u64, size: u32) -> AccessPattern;

    /// Reset internal state (e.g. on file close, or on engine wishing
    /// to restart pattern hunting).
    fn reset(&mut self);
}
