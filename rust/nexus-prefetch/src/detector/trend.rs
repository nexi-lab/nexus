//! Leap-style majority-trend detector (ATC'20 §3).  Keeps a sliding
//! window of the last N signed deltas; if more than half are equal to
//! some value `d`, emits `Trend { delta: d }`.  Tolerates jitter that
//! a strict stride detector would reject.

use crate::detector::Detector;
use crate::pattern::AccessPattern;
use std::collections::HashMap;

const WINDOW: usize = 8;

pub struct MajorityTrendDetector {
    last_offset: Option<u64>,
    deltas: std::collections::VecDeque<i64>,
}

impl Default for MajorityTrendDetector {
    fn default() -> Self {
        Self::new()
    }
}

impl MajorityTrendDetector {
    pub fn new() -> Self {
        Self {
            last_offset: None,
            deltas: std::collections::VecDeque::with_capacity(WINDOW),
        }
    }
}

impl Detector for MajorityTrendDetector {
    fn observe(&mut self, offset: u64, _size: u32) -> AccessPattern {
        let Some(prev) = self.last_offset else {
            self.last_offset = Some(offset);
            return AccessPattern::Cold;
        };
        let delta = offset as i64 - prev as i64;
        self.last_offset = Some(offset);

        if self.deltas.len() == WINDOW {
            self.deltas.pop_front();
        }
        self.deltas.push_back(delta);

        if self.deltas.len() < WINDOW / 2 {
            return AccessPattern::Cold;
        }

        let mut counts: HashMap<i64, usize> = HashMap::new();
        for d in &self.deltas {
            *counts.entry(*d).or_insert(0) += 1;
        }
        let (best_delta, best_count) = counts
            .iter()
            .max_by_key(|(_, c)| **c)
            .map(|(d, c)| (*d, *c))
            .unwrap();

        if best_count * 2 > self.deltas.len() {
            AccessPattern::Trend { delta: best_delta }
        } else {
            AccessPattern::Random
        }
    }

    fn reset(&mut self) {
        self.last_offset = None;
        self.deltas.clear();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_observation_is_cold() {
        let mut d = MajorityTrendDetector::new();
        assert_eq!(d.observe(0, 4096), AccessPattern::Cold);
    }

    #[test]
    fn dominant_delta_wins_with_jitter() {
        let mut d = MajorityTrendDetector::new();
        // Offsets: 0, 4096, 8192, 12288, 16384, 99999 (noise), 24576, 28672
        // Deltas: +4096 ×5, +83615, +4097 — majority is +4096.
        for off in [0u64, 4096, 8192, 12288, 16384, 99999, 24576, 28672, 32768] {
            let _ = d.observe(off, 4096);
        }
        // After WINDOW=8 deltas accumulated, +4096 occurs 5×, majority.
        assert!(matches!(
            d.observe(36864, 4096),
            AccessPattern::Trend { delta: 4096 }
        ));
    }

    #[test]
    fn random_offsets_yield_random_pattern() {
        let mut d = MajorityTrendDetector::new();
        // Random offsets — no delta repeats.
        for off in [
            0u64, 100, 1_000_000, 42, 999_999, 17, 314_159, 27_182, 99_991,
        ] {
            let _ = d.observe(off, 4096);
        }
        // Tenth observation: no delta has majority.
        assert_eq!(d.observe(424_242, 4096), AccessPattern::Random);
    }

    #[test]
    fn reset_clears_state() {
        let mut d = MajorityTrendDetector::new();
        for off in (0..10).map(|i| i * 4096) {
            d.observe(off, 4096);
        }
        d.reset();
        assert_eq!(d.observe(123, 4096), AccessPattern::Cold);
    }
}
