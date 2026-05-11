//! Error type for the prefetch engine.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum PrefetchError {
    #[error("backend read failed: {0}")]
    Backend(String),
    #[error("prefetch queue full — dropping hint")]
    QueueFull,
    #[error("engine shutting down")]
    Shutdown,
    #[error("offset {offset} + size {size} exceeds file bounds {file_size}")]
    OutOfRange {
        offset: u64,
        size: u32,
        file_size: u64,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn display_includes_offset_for_out_of_range() {
        let e = PrefetchError::OutOfRange {
            offset: 1024,
            size: 512,
            file_size: 1000,
        };
        let s = format!("{e}");
        assert!(s.contains("1024"));
        assert!(s.contains("512"));
        assert!(s.contains("1000"));
    }

    #[test]
    fn backend_error_preserves_message() {
        let e = PrefetchError::Backend("net timeout".into());
        assert!(format!("{e}").contains("net timeout"));
    }
}
