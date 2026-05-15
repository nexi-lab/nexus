use crate::{
    error::BenchResult,
    metrics::OperationMetrics,
    trace::{OpKind, TraceOp},
};

use super::BenchTarget;

#[derive(Debug, Clone, Copy, Default)]
pub struct NoopTarget;

impl BenchTarget for NoopTarget {
    fn name(&self) -> &'static str {
        "noop"
    }

    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
        let logical_bytes_read = op.logical_read_len();
        let logical_bytes_written = op.logical_write_len();
        let egress_bytes = if op.op == OpKind::Read {
            logical_bytes_read
        } else {
            0
        };
        Ok(OperationMetrics {
            logical_bytes_read,
            logical_bytes_written,
            rpc_count: 1,
            egress_bytes,
            cache_hit: None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{OpKind, TraceOp};

    #[test]
    fn noop_counts_logical_bytes() {
        let target = NoopTarget;
        let op = TraceOp {
            timestamp_ns: 0,
            op: OpKind::Read,
            path: "/file".to_string(),
            to_path: None,
            offset: Some(0),
            length: Some(4096),
            payload_seed: None,
            parallel_group: None,
        };
        let metrics = target.execute(&op).expect("noop target should succeed");
        assert_eq!(metrics.logical_bytes_read, 4096);
        assert_eq!(metrics.logical_bytes_written, 0);
        assert_eq!(metrics.rpc_count, 1);
        assert_eq!(metrics.egress_bytes, 4096);
        assert_eq!(metrics.cache_hit, None);
    }
}
