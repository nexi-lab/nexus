//! Stub for stride detector.

use crate::detector::Detector;
use crate::pattern::AccessPattern;

pub struct StrideDetector;

impl Detector for StrideDetector {
    fn observe(&mut self, _offset: u64, _size: u32) -> AccessPattern { AccessPattern::Cold }
    fn reset(&mut self) {}
}
