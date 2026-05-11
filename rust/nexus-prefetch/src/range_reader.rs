//! Abstraction over backend range reads.  The engine calls this for
//! every prefetch block.  Real impls wrap `ObjectStore::read_range`
//! (Rust kernel) or a Python callable (pyo3 bridge); tests use the
//! `MockRangeReader` below.

use crate::error::PrefetchError;
use bytes::Bytes;
use std::sync::Arc;

pub trait RangeReader: Send + Sync + 'static {
    fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError>;
}

/// Boilerplate-free `Arc` alias for the engine's stored reader.
pub type SharedRangeReader = Arc<dyn RangeReader>;

#[cfg(test)]
pub mod mock {
    use super::*;
    use parking_lot::Mutex;

    pub struct MockRangeReader {
        pub data: Bytes,
        pub call_log: Mutex<Vec<(String, u64, u32)>>,
    }

    impl MockRangeReader {
        pub fn new(data: Bytes) -> Self {
            Self {
                data,
                call_log: Mutex::new(Vec::new()),
            }
        }
    }

    impl RangeReader for MockRangeReader {
        fn read(&self, key: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError> {
            self.call_log.lock().push((key.to_string(), offset, size));
            let end = (offset + size as u64).min(self.data.len() as u64) as usize;
            let start = offset as usize;
            if start >= self.data.len() {
                return Err(PrefetchError::OutOfRange {
                    offset,
                    size,
                    file_size: self.data.len() as u64,
                });
            }
            Ok(self.data.slice(start..end))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use mock::MockRangeReader;

    #[test]
    fn mock_returns_slice() {
        let r = MockRangeReader::new(Bytes::from(vec![1u8, 2, 3, 4, 5, 6, 7, 8]));
        let out = r.read("x", 2, 4).unwrap();
        assert_eq!(&out[..], &[3, 4, 5, 6]);
    }

    #[test]
    fn mock_out_of_range_errors() {
        let r = MockRangeReader::new(Bytes::from(vec![1u8; 4]));
        assert!(matches!(
            r.read("x", 10, 4),
            Err(PrefetchError::OutOfRange { .. })
        ));
    }

    #[test]
    fn mock_logs_calls() {
        let r = MockRangeReader::new(Bytes::from(vec![1u8; 16]));
        let _ = r.read("a", 0, 4);
        let _ = r.read("b", 4, 4);
        let log = r.call_log.lock();
        assert_eq!(log.len(), 2);
        assert_eq!(log[0], ("a".to_string(), 0, 4));
        assert_eq!(log[1], ("b".to_string(), 4, 4));
    }
}
