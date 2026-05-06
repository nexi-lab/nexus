use std::{thread, time::Instant};

use crate::{
    error::{BenchError, BenchResult},
    metrics::OperationSample,
    target::BenchTarget,
    trace::TraceOp,
};

pub fn run_trace<T: BenchTarget + Clone + 'static>(
    target: &T,
    trace: &[TraceOp],
    allow_errors: bool,
) -> BenchResult<Vec<OperationSample>> {
    let mut samples = Vec::with_capacity(trace.len());
    let mut index = 0;

    while index < trace.len() {
        if let Some(group) = non_empty_group(&trace[index]) {
            let start = index;
            while index < trace.len()
                && non_empty_group(&trace[index]).as_deref() == Some(group.as_str())
            {
                index += 1;
            }
            let mut grouped = run_parallel_group(target.clone(), &trace[start..index]);
            if !allow_errors {
                if let Some(error) = grouped.iter().find_map(|sample| sample.error.clone()) {
                    return Err(BenchError::Target(error));
                }
            }
            samples.append(&mut grouped);
        } else {
            let sample = run_one(target, &trace[index]);
            if !allow_errors {
                if let Some(error) = sample.error.clone() {
                    return Err(BenchError::Target(error));
                }
            }
            samples.push(sample);
            index += 1;
        }
    }

    Ok(samples)
}

fn run_parallel_group<T: BenchTarget + Clone + 'static>(
    target: T,
    ops: &[TraceOp],
) -> Vec<OperationSample> {
    let handles: Vec<_> = ops
        .iter()
        .cloned()
        .map(|op| {
            let target = target.clone();
            thread::spawn(move || run_one(&target, &op))
        })
        .collect();
    handles
        .into_iter()
        .map(|handle| {
            handle.join().unwrap_or_else(|_| {
                OperationSample::failure(
                    crate::trace::OpKind::Lookup,
                    Default::default(),
                    "parallel worker panicked".to_string(),
                )
            })
        })
        .collect()
}

fn run_one<T: BenchTarget>(target: &T, op: &TraceOp) -> OperationSample {
    let started = Instant::now();
    match target.execute(op) {
        Ok(metrics) => OperationSample::success(op.op, started.elapsed(), metrics),
        Err(err) => OperationSample::failure(op.op, started.elapsed(), err.to_string()),
    }
}

fn non_empty_group(op: &TraceOp) -> Option<String> {
    op.parallel_group
        .as_ref()
        .filter(|group| !group.is_empty())
        .cloned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        target::noop::NoopTarget,
        trace::{OpKind, TraceOp},
    };

    fn read_op(path: &str, group: Option<&str>) -> TraceOp {
        TraceOp {
            timestamp_ns: 0,
            op: OpKind::Read,
            path: path.to_string(),
            to_path: None,
            offset: Some(0),
            length: Some(10),
            payload_seed: None,
            parallel_group: group.map(str::to_string),
        }
    }

    #[test]
    fn runner_records_successful_samples() {
        let trace = vec![read_op("/a", None), read_op("/b", None)];
        let samples = run_trace(&NoopTarget, &trace, false).expect("runner should succeed");
        assert_eq!(samples.len(), 2);
        assert!(samples.iter().all(|sample| sample.error.is_none()));
        assert_eq!(
            samples
                .iter()
                .map(|sample| sample.metrics.logical_bytes_read)
                .sum::<u64>(),
            20
        );
    }

    #[test]
    fn runner_handles_adjacent_parallel_groups() {
        let trace = vec![
            read_op("/a", Some("group-1")),
            read_op("/b", Some("group-1")),
            read_op("/c", None),
        ];
        let samples = run_trace(&NoopTarget, &trace, false).expect("parallel group should succeed");
        assert_eq!(samples.len(), 3);
    }
}
