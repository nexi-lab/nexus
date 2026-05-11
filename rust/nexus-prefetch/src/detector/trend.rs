//! Stub for majority-trend detector.

use crate::detector::Detector;
use crate::pattern::AccessPattern;

pub struct MajorityTrendDetector;

impl Detector for MajorityTrendDetector {
    fn observe(&mut self, _offset: u64, _size: u32) -> AccessPattern { AccessPattern::Cold }
    fn reset(&mut self) {}
}
