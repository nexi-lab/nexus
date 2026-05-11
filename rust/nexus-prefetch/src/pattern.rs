//! Detector output — what kind of access pattern the latest observation
//! suggests.  Drives the prefetch decision in the engine.

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AccessPattern {
    /// No prefetch should be issued yet.
    Cold,
    /// Sequential forward — prefetcher should issue the next N blocks.
    Sequential,
    /// Fixed stride detected — prefetcher should issue blocks at
    /// `offset + k*stride` for k=1..max_blocks.
    Stride { stride: i64 },
    /// Leap-style majority trend — issue prefetches along the dominant
    /// delta direction.
    Trend { delta: i64 },
    /// Random — prefetcher must reset window and drop pending.
    Random,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patterns_compare_by_value() {
        assert_eq!(AccessPattern::Sequential, AccessPattern::Sequential);
        assert_ne!(
            AccessPattern::Stride { stride: 4096 },
            AccessPattern::Stride { stride: 8192 }
        );
    }
}
