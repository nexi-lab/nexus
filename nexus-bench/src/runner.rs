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
            let mut grouped = run_parallel_group(target.clone(), start, &trace[start..index]);
            if !allow_errors {
                if let Some(error) = grouped.iter().find_map(|sample| sample.error.clone()) {
                    return Err(BenchError::Target(error));
                }
            }
            samples.append(&mut grouped);
        } else {
            let sample = run_one(target, index, &trace[index]);
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
    start_index: usize,
    ops: &[TraceOp],
) -> Vec<OperationSample> {
    let handles: Vec<_> = ops
        .iter()
        .cloned()
        .enumerate()
        .map(|(offset, op)| {
            let target = target.clone();
            let index = start_index + offset;
            let panic_op = op.clone();
            (
                index,
                panic_op,
                thread::spawn(move || run_one(&target, index, &op)),
            )
        })
        .collect();
    handles
        .into_iter()
        .map(|(index, op, handle)| {
            handle.join().unwrap_or_else(|_| {
                OperationSample::failure(
                    op.op,
                    Default::default(),
                    format_operation_error(index, &op, "parallel worker panicked"),
                )
            })
        })
        .collect()
}

fn run_one<T: BenchTarget>(target: &T, index: usize, op: &TraceOp) -> OperationSample {
    let started = Instant::now();
    match target.execute(op) {
        Ok(metrics) => OperationSample::success(op.op, started.elapsed(), metrics),
        Err(err) => OperationSample::failure(
            op.op,
            started.elapsed(),
            format_operation_error(index, op, err),
        ),
    }
}

fn non_empty_group(op: &TraceOp) -> Option<String> {
    op.parallel_group
        .as_ref()
        .filter(|group| !group.is_empty())
        .cloned()
}

fn format_operation_error(index: usize, op: &TraceOp, error: impl std::fmt::Display) -> String {
    format!(
        "operation failed at index {index} ({:?} {}): {error}",
        op.op, op.path
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        error::{BenchError, BenchResult},
        metrics::OperationMetrics,
        target::noop::NoopTarget,
        trace::{OpKind, TraceOp},
    };
    use std::{
        sync::{Arc, Mutex},
        thread,
        time::Duration,
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

    #[derive(Clone)]
    struct FailingTarget {
        fail_path: &'static str,
    }

    impl BenchTarget for FailingTarget {
        fn name(&self) -> &'static str {
            "failing"
        }

        fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
            if op.path == self.fail_path {
                return Err(BenchError::Target("target failed".to_string()));
            }
            Ok(OperationMetrics {
                logical_bytes_read: op.logical_read_len(),
                logical_bytes_written: op.logical_write_len(),
                rpc_count: 1,
                egress_bytes: 0,
                cache_hit: None,
            })
        }
    }

    #[derive(Clone)]
    struct PanickingTarget;

    impl BenchTarget for PanickingTarget {
        fn name(&self) -> &'static str {
            "panicking"
        }

        fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
            if op.path == "/panic" {
                panic!("worker panic");
            }
            Ok(OperationMetrics::default())
        }
    }

    #[derive(Clone)]
    struct RecordingTarget {
        events: Arc<Mutex<Vec<String>>>,
    }

    impl BenchTarget for RecordingTarget {
        fn name(&self) -> &'static str {
            "recording"
        }

        fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
            if op.path == "/a" {
                thread::sleep(Duration::from_millis(10));
            }
            self.events.lock().unwrap().push(op.path.clone());
            Ok(OperationMetrics {
                logical_bytes_read: op.length.unwrap_or(0),
                logical_bytes_written: 0,
                rpc_count: 1,
                egress_bytes: 0,
                cache_hit: None,
            })
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

    #[test]
    fn runner_records_failures_and_continues_when_allowed() {
        let trace = vec![
            read_op("/a", None),
            read_op("/fail", None),
            read_op("/c", None),
        ];
        let samples = run_trace(&FailingTarget { fail_path: "/fail" }, &trace, true)
            .expect("allow_errors should collect failures");

        assert_eq!(samples.len(), 3);
        assert!(samples[0].error.is_none());
        assert!(samples[2].error.is_none());
        let error = samples[1].error.as_ref().expect("failed sample");
        assert!(error.contains("index 1"));
        assert!(error.contains("Read"));
        assert!(error.contains("/fail"));
        assert!(error.contains("target failed"));
    }

    #[test]
    fn runner_fail_fast_error_includes_operation_context() {
        let trace = vec![
            read_op("/a", None),
            read_op("/fail", None),
            read_op("/c", None),
        ];
        let error = run_trace(&FailingTarget { fail_path: "/fail" }, &trace, false)
            .expect_err("fail-fast should stop on target failure");
        let error = error.to_string();

        assert!(error.contains("index 1"));
        assert!(error.contains("Read"));
        assert!(error.contains("/fail"));
        assert!(error.contains("target failed"));
    }

    #[test]
    fn parallel_worker_panic_preserves_operation_context() {
        let mut panic_op = read_op("/panic", Some("group-1"));
        panic_op.op = OpKind::Write;
        let trace = vec![read_op("/a", Some("group-1")), panic_op];
        let samples = run_trace(&PanickingTarget, &trace, true).expect("panic should be recorded");

        assert_eq!(samples.len(), 2);
        assert_eq!(samples[1].op, OpKind::Write);
        let error = samples[1].error.as_ref().expect("panic sample");
        assert!(error.contains("index 1"));
        assert!(error.contains("Write"));
        assert!(error.contains("/panic"));
        assert!(error.contains("parallel worker panicked"));
    }

    #[test]
    fn adjacent_parallel_groups_keep_trace_order_without_merging() {
        let mut trace = vec![
            read_op("/a", Some("group-1")),
            read_op("/b", Some("group-1")),
            read_op("/c", Some("group-2")),
            read_op("/d", Some("group-2")),
        ];
        for (index, op) in trace.iter_mut().enumerate() {
            op.length = Some(index as u64 + 1);
        }
        let events = Arc::new(Mutex::new(Vec::new()));
        let samples = run_trace(
            &RecordingTarget {
                events: Arc::clone(&events),
            },
            &trace,
            false,
        )
        .expect("parallel groups should succeed");

        assert_eq!(samples.len(), 4);
        assert_eq!(
            samples
                .iter()
                .map(|sample| sample.metrics.logical_bytes_read)
                .collect::<Vec<_>>(),
            vec![1, 2, 3, 4]
        );
        let events = events.lock().unwrap().clone();
        let group_1_finished = events.iter().position(|path| path == "/a").unwrap().max(
            events
                .iter()
                .position(|path| path == "/b")
                .expect("/b should execute"),
        );
        let group_2_started = events
            .iter()
            .position(|path| path == "/c" || path == "/d")
            .expect("group-2 should execute");
        assert!(
            group_1_finished < group_2_started,
            "group-2 started before group-1 finished: {events:?}"
        );
    }
}
