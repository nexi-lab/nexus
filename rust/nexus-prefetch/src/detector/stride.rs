//! Fixed-stride detector.  Tracks the last two offsets; computes the
//! signed delta; if the next observation matches that delta (within
//! 1 block), emits `Stride { stride }`.  Three confirmations required
//! before `Stride` fires — single matches are still `Cold`.

use crate::detector::Detector;
use crate::pattern::AccessPattern;

const MIN_CONFIRMATIONS: u32 = 3;

pub struct StrideDetector {
    history: [Option<u64>; 3], // ring buffer of last 3 offsets
    confirmations: u32,
    last_stride: Option<i64>,
}

impl Default for StrideDetector {
    fn default() -> Self {
        Self::new()
    }
}

impl StrideDetector {
    pub fn new() -> Self {
        Self {
            history: [None, None, None],
            confirmations: 0,
            last_stride: None,
        }
    }

    fn push(&mut self, offset: u64) {
        self.history.rotate_left(1);
        self.history[2] = Some(offset);
    }
}

impl Detector for StrideDetector {
    fn observe(&mut self, offset: u64, _size: u32) -> AccessPattern {
        self.push(offset);

        let (Some(a), Some(b), Some(c)) = (self.history[0], self.history[1], self.history[2])
        else {
            return AccessPattern::Cold;
        };

        let s1 = b as i64 - a as i64;
        let s2 = c as i64 - b as i64;
        if s1 == 0 || s2 == 0 {
            self.confirmations = 0;
            self.last_stride = None;
            return AccessPattern::Cold;
        }

        if s1 == s2 {
            if self.last_stride == Some(s1) {
                self.confirmations += 1;
            } else {
                self.last_stride = Some(s1);
                self.confirmations = 1;
            }
            if self.confirmations >= MIN_CONFIRMATIONS - 1 {
                return AccessPattern::Stride { stride: s1 };
            }
            return AccessPattern::Cold;
        }

        self.confirmations = 0;
        self.last_stride = None;
        AccessPattern::Random
    }

    fn reset(&mut self) {
        self.history = [None, None, None];
        self.confirmations = 0;
        self.last_stride = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_stride_detected_after_three_confirms() {
        let mut d = StrideDetector::new();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold); // history: [_, _, 0]
        assert_eq!(d.observe(8192, 4096), AccessPattern::Cold); // [_, 0, 8192]
                                                                // Third observation: triple (0, 8192, 16384) → s1=s2=8192, confirmations=1
        assert_eq!(d.observe(16384, 4096), AccessPattern::Cold);
        // Fourth: triple (8192, 16384, 24576) → s1=s2=8192, confirmations=2 → Stride
        assert_eq!(
            d.observe(24576, 4096),
            AccessPattern::Stride { stride: 8192 }
        );
    }

    #[test]
    fn changing_stride_resets_to_random() {
        let mut d = StrideDetector::new();
        d.observe(0, 4096);
        d.observe(4096, 4096);
        d.observe(8192, 4096); // s1=s2=4096, conf=1, Cold
                               // Now break: jump by 16 KiB.
        assert_eq!(d.observe(8192 + 16 * 1024, 4096), AccessPattern::Random);
    }

    #[test]
    fn negative_stride_detected() {
        let mut d = StrideDetector::new();
        d.observe(100_000, 4096);
        d.observe(90_000, 4096);
        d.observe(80_000, 4096); // s1=s2=-10000, conf=1
        assert_eq!(
            d.observe(70_000, 4096),
            AccessPattern::Stride { stride: -10_000 }
        );
    }

    #[test]
    fn zero_stride_is_not_a_pattern() {
        let mut d = StrideDetector::new();
        d.observe(1000, 4096);
        d.observe(1000, 4096);
        // s1=0 — should never emit Stride { stride: 0 }
        let p = d.observe(1000, 4096);
        assert!(matches!(p, AccessPattern::Cold));
    }
}
