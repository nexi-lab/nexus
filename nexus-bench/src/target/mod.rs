pub mod http;
pub mod mount;
pub mod noop;

use crate::{error::BenchResult, metrics::OperationMetrics, trace::TraceOp};

pub trait BenchTarget: Send + Sync {
    fn name(&self) -> &'static str;
    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics>;
}
