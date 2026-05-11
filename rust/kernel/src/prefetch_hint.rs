//! Kernel-side prefetch hint emission.  The kernel doesn't own the
//! engine — the cdylib does — so this trait is the cut.  A concrete
//! sink is installed from Python after the engine is constructed.

pub trait PrefetchHintSink: Send + Sync {
    /// Notify that a DT_REG read just completed.  The sink is expected
    /// to be cheap (≤ 100 ns); never block.
    fn on_read(&self, path: &str, offset: u64, size: u32);
}

/// No-op sink — used when prefetch is disabled or not yet installed.
pub struct NullSink;

impl PrefetchHintSink for NullSink {
    fn on_read(&self, _: &str, _: u64, _: u32) {}
}

#[cfg(test)]
mod tests {
    use super::*;
    use parking_lot::Mutex;
    use std::sync::Arc;

    struct RecordingSink(Mutex<Vec<(String, u64, u32)>>);

    impl PrefetchHintSink for RecordingSink {
        fn on_read(&self, path: &str, off: u64, sz: u32) {
            self.0.lock().push((path.to_string(), off, sz));
        }
    }

    #[test]
    fn null_sink_is_a_noop() {
        let s = NullSink;
        s.on_read("/x", 0, 4); // no panic, no crash
    }

    #[test]
    fn recording_sink_captures_call() {
        let s = Arc::new(RecordingSink(Mutex::new(vec![])));
        (s.as_ref() as &dyn PrefetchHintSink).on_read("/p", 8, 16);
        let log = s.0.lock();
        assert_eq!(log.len(), 1);
        assert_eq!(log[0], ("/p".to_string(), 8, 16));
    }
}
