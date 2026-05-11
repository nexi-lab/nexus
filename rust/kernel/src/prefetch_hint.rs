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

/// Adapter — wraps a `nexus_prefetch::PrefetchEngine` into a sink.
/// The engine internally tracks per-fh state by file handle, but the
/// kernel only knows the *path* at the sys_read level.  We therefore
/// key the engine sessions by a per-path synthetic fh handed out on
/// first observation.  This is a one-way fire-and-forget channel —
/// the kernel can emit hints but does not consume them.
pub struct KernelEngineSink {
    engine: std::sync::Arc<nexus_prefetch::PrefetchEngine>,
    path_to_fh: dashmap::DashMap<String, u64>,
    next_fh: std::sync::atomic::AtomicU64,
}

impl KernelEngineSink {
    pub fn new(engine: std::sync::Arc<nexus_prefetch::PrefetchEngine>) -> Self {
        Self {
            engine,
            path_to_fh: dashmap::DashMap::new(),
            next_fh: std::sync::atomic::AtomicU64::new(1),
        }
    }

    fn fh_for(&self, path: &str) -> u64 {
        if let Some(v) = self.path_to_fh.get(path) {
            return *v;
        }
        let fh = self
            .next_fh
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        self.path_to_fh.insert(path.to_string(), fh);
        self.engine.on_open(fh, path, None);
        fh
    }
}

impl PrefetchHintSink for KernelEngineSink {
    fn on_read(&self, path: &str, offset: u64, size: u32) {
        let fh = self.fh_for(path);
        let _ = self.engine.on_read(fh, offset, size);
    }
}

#[cfg(test)]
mod engine_sink_tests {
    use super::*;
    use bytes::Bytes;
    use nexus_prefetch::{EngineConfig, PrefetchEngine, PrefetchError, RangeReader};
    use std::sync::Arc;

    struct Noop;
    impl RangeReader for Noop {
        fn read(&self, _: &str, _: u64, _: u32) -> Result<Bytes, PrefetchError> {
            Ok(Bytes::from_static(b""))
        }
    }

    #[test]
    fn kernel_sink_opens_session_lazily_on_first_hint() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let engine = Arc::new(PrefetchEngine::new(
            EngineConfig::default(),
            Arc::new(Noop) as Arc<dyn RangeReader>,
            Some(rt),
        ));
        let sink = KernelEngineSink::new(engine.clone());
        sink.on_read("/x", 0, 4096);
        sink.on_read("/x", 4096, 4096);
        // No assertion on outcome — metrics race; the assertion is that
        // it didn't panic and lazily opened a session.
        assert!(sink.path_to_fh.contains_key("/x"));
    }

    #[test]
    fn kernel_sink_assigns_distinct_fhs_to_distinct_paths() {
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let engine = Arc::new(PrefetchEngine::new(
            EngineConfig::default(),
            Arc::new(Noop) as Arc<dyn RangeReader>,
            Some(rt),
        ));
        let sink = KernelEngineSink::new(engine);
        sink.on_read("/a", 0, 4);
        sink.on_read("/b", 0, 4);
        let fh_a = *sink.path_to_fh.get("/a").unwrap();
        let fh_b = *sink.path_to_fh.get("/b").unwrap();
        assert_ne!(fh_a, fh_b);
    }
}
