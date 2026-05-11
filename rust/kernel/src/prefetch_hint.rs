//! Kernel-side prefetch hint emission contract.
//!
//! The kernel publishes a read-event stream via this trait; a real
//! sink that forwards into `nexus_prefetch::PrefetchEngine` is NOT
//! wired up in production today (Issue #4057 adversarial review #2 —
//! the original `KernelEngineSink` had a first-open race and never
//! released sessions, so it was removed).  The default `NullSink`
//! makes hint emission a no-op cheap enough to leave in the read
//! path; a future change can install a real sink with explicit
//! `release` semantics through a Python-callable setter once the
//! Python `ReadaheadManager` is no longer the canonical fan-out
//! point.

pub trait PrefetchHintSink: Send + Sync {
    /// Notify that a DT_REG read just completed.  The sink is expected
    /// to be cheap (≤ 100 ns); never block.
    fn on_read(&self, path: &str, offset: u64, size: u32);
}

/// No-op sink — the only impl shipped today.  Installed by default in
/// `Kernel::new` so the emission sites can call `on_read` unconditionally
/// without checking for `Option<>`.
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
