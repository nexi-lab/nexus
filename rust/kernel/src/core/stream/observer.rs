//! StreamEventObserver — publishes FileEvents to DT_STREAM.
//!
//! Generic observer: on mutation, serializes FileEvent to JSON and writes
//! to a configurable DT_STREAM path. Multiple instances can be registered
//! with different paths for different consumers (remote watch, event bus,
//! workflow triggers, Zoekt indexer, etc.).
//!
//! Pure Rust. No Python. ~0.5μs per write (MemoryStreamBackend).

use crate::dispatch::{FileEvent, MutationObserver};
use crate::stream_manager::StreamManager;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

/// Default stream capacity.
#[allow(dead_code)]
const DEFAULT_CAPACITY: usize = 8192;

/// Rust-native observer: serialize FileEvent → JSON → DT_STREAM.
///
/// Registered on-demand by orchestrator at boot time.
/// dispatch_observers calls on_mutation inline. Pure Rust, safe Drop.
#[allow(dead_code)]
pub(crate) struct StreamEventObserver {
    stream_manager: Arc<StreamManager>,
    stream_path: String,
    capacity: usize,
    initialized: AtomicBool,
}

#[allow(dead_code)]
impl StreamEventObserver {
    /// Create observer writing to the given stream path.
    pub(crate) fn new(stream_manager: Arc<StreamManager>, path: impl Into<String>) -> Self {
        Self {
            stream_manager,
            stream_path: path.into(),
            capacity: DEFAULT_CAPACITY,
            initialized: AtomicBool::new(false),
        }
    }

    fn ensure_stream(&self) {
        if self.initialized.load(Ordering::Acquire) {
            return;
        }
        let _ = self.stream_manager.create(&self.stream_path, self.capacity);
        self.initialized.store(true, Ordering::Release);
    }
}

impl MutationObserver for StreamEventObserver {
    fn on_mutation(&self, event: &FileEvent) {
        self.ensure_stream();
        let json = event.to_json();
        let _ = self
            .stream_manager
            .write_nowait(&self.stream_path, json.as_bytes());
    }
}
