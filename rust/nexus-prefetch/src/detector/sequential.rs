//! Stub for sequential detector.

use crate::detector::Detector;
use crate::pattern::AccessPattern;

pub struct SequentialDetector;

impl Detector for SequentialDetector {
    fn observe(&mut self, _offset: u64, _size: u32) -> AccessPattern { AccessPattern::Cold }
    fn reset(&mut self) {}
}
