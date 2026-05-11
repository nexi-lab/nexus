//! Forward-sequential detector — mirrors the logic at
//! `src/nexus/fuse/readahead.py:194–235`.
//!
//! State: last (offset, size), sequential_count.  A read is sequential
//! if its offset lies within `tolerance` of `last_offset + last_size`
//! AND does not move backward.  After `min_sequential_count`
//! consecutive sequential observations we emit `Sequential`; otherwise
//! `Cold` (warming up) or `Random` (broken stream).

use crate::detector::Detector;
use crate::pattern::AccessPattern;

pub struct SequentialDetector {
    last_offset: u64,
    last_size: u32,
    sequential_count: u32,
    tolerance: u64,
    min_sequential_count: u32,
    initialised: bool,
}

impl SequentialDetector {
    pub fn new(tolerance: u64, min_sequential_count: u32) -> Self {
        Self {
            last_offset: 0,
            last_size: 0,
            sequential_count: 0,
            tolerance,
            min_sequential_count,
            initialised: false,
        }
    }
}

impl Detector for SequentialDetector {
    fn observe(&mut self, offset: u64, size: u32) -> AccessPattern {
        if !self.initialised {
            self.last_offset = offset;
            self.last_size = size;
            self.initialised = true;
            return AccessPattern::Cold;
        }

        let expected = self.last_offset.saturating_add(self.last_size as u64);
        let signed_diff = offset as i64 - expected as i64;
        let forward = offset >= self.last_offset;
        let within_tol = signed_diff.unsigned_abs() <= self.tolerance;

        let pattern = if forward && within_tol {
            self.sequential_count += 1;
            if self.sequential_count >= self.min_sequential_count {
                AccessPattern::Sequential
            } else {
                AccessPattern::Cold
            }
        } else {
            self.sequential_count = 0;
            AccessPattern::Random
        };

        self.last_offset = offset;
        self.last_size = size;
        pattern
    }

    fn reset(&mut self) {
        self.last_offset = 0;
        self.last_size = 0;
        self.sequential_count = 0;
        self.initialised = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn det() -> SequentialDetector {
        SequentialDetector::new(64 * 1024, 2)
    }

    #[test]
    fn first_observation_is_cold() {
        let mut d = det();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold);
    }

    #[test]
    fn two_consecutive_sequential_reads_trigger_sequential() {
        let mut d = det();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold);
        assert_eq!(d.observe(4096, 4096), AccessPattern::Cold); // count=1, below threshold
        assert_eq!(d.observe(8192, 4096), AccessPattern::Sequential); // count=2
    }

    #[test]
    fn within_tolerance_still_sequential() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096); // count=1
                               // Next read at 8192 + 32 KiB skip (< 64 KiB tolerance) — still seq.
        assert_eq!(d.observe(8192 + 32 * 1024, 4096), AccessPattern::Sequential);
    }

    #[test]
    fn backward_jump_is_random_and_resets() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        d.observe(8192, 4096);
        // Backward jump
        assert_eq!(d.observe(0, 4096), AccessPattern::Random);
        // Counter resets — next sequential pair must re-prime
        assert_eq!(d.observe(4096, 4096), AccessPattern::Cold);
        assert_eq!(d.observe(8192, 4096), AccessPattern::Sequential);
    }

    #[test]
    fn large_forward_gap_beyond_tolerance_is_random() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        // Jump 10 MiB ahead — far beyond 64 KiB tolerance.
        assert_eq!(d.observe(10 * 1024 * 1024, 4096), AccessPattern::Random);
    }

    #[test]
    fn reset_clears_state() {
        let mut d = det();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        d.reset();
        assert_eq!(d.observe(1024, 4096), AccessPattern::Cold);
    }
}
