//! Adapter that lets `nexus-prefetch` issue range GETs through the
//! kernel's `ObjectStore` pillar.  Wraps a `(Backend, OperationContext)`
//! pair and implements `RangeReader`.

use crate::abc::object_store::ObjectStore;
use crate::kernel::OperationContext;
use bytes::Bytes;
use nexus_prefetch::{PrefetchError, RangeReader};
use std::sync::Arc;

pub struct KernelRangeReader {
    backend: Arc<dyn ObjectStore>,
    ctx: OperationContext,
}

impl KernelRangeReader {
    pub fn new(backend: Arc<dyn ObjectStore>, ctx: OperationContext) -> Self {
        Self { backend, ctx }
    }
}

impl RangeReader for KernelRangeReader {
    fn read(&self, content_id: &str, offset: u64, size: u32) -> Result<Bytes, PrefetchError> {
        self.backend
            .read_range(content_id, offset, size, &self.ctx)
            .map(Bytes::from)
            .map_err(|e| PrefetchError::Backend(format!("{e:?}")))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::abc::object_store::{StorageError, WriteResult};

    struct ConstBackend(Vec<u8>);

    impl ObjectStore for ConstBackend {
        fn name(&self) -> &str {
            "const"
        }

        fn write_content(
            &self,
            _content: &[u8],
            _content_id: &str,
            _ctx: &OperationContext,
            _offset: u64,
        ) -> Result<WriteResult, StorageError> {
            Err(StorageError::NotSupported("write_content"))
        }

        fn read_content(
            &self,
            _content_id: &str,
            _ctx: &OperationContext,
        ) -> Result<Vec<u8>, StorageError> {
            Ok(self.0.clone())
        }
    }

    #[test]
    fn adapter_returns_slice() {
        let backend: Arc<dyn ObjectStore> = Arc::new(ConstBackend((0u8..16).collect()));
        let ctx = OperationContext::new("test", "test", false, None, true);
        let r = KernelRangeReader::new(backend, ctx);
        let out = <KernelRangeReader as RangeReader>::read(&r, "x", 4, 8).unwrap();
        assert_eq!(&out[..], &(4u8..12).collect::<Vec<_>>()[..]);
    }

    #[test]
    fn adapter_handles_empty_response() {
        let backend: Arc<dyn ObjectStore> = Arc::new(ConstBackend(vec![1u8; 4]));
        let ctx = OperationContext::new("test", "test", false, None, true);
        let r = KernelRangeReader::new(backend, ctx);
        // Past EOF — backend returns empty via default read_range impl from Task 16.
        let out = <KernelRangeReader as RangeReader>::read(&r, "x", 100, 4).unwrap();
        assert!(out.is_empty());
    }
}
