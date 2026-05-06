use std::{fs, path::Path};

use serde::{Deserialize, Serialize};

use crate::error::{BenchError, BenchResult};

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum OpKind {
    Read,
    Write,
    Getattr,
    Lookup,
    Readdir,
    Delete,
    Rename,
    Mkdir,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TraceOp {
    pub timestamp_ns: u64,
    pub op: OpKind,
    pub path: String,
    #[serde(default)]
    pub to_path: Option<String>,
    #[serde(default)]
    pub offset: Option<u64>,
    #[serde(default)]
    pub length: Option<u64>,
    #[serde(default)]
    pub payload_seed: Option<u64>,
    #[serde(default)]
    pub parallel_group: Option<String>,
}

impl TraceOp {
    pub fn logical_read_len(&self) -> u64 {
        if self.op == OpKind::Read {
            self.length.unwrap_or(0)
        } else {
            0
        }
    }

    pub fn logical_write_len(&self) -> u64 {
        if self.op == OpKind::Write {
            self.length.unwrap_or(0)
        } else {
            0
        }
    }
}

pub fn load_trace(path: &Path) -> BenchResult<Vec<TraceOp>> {
    let bytes = fs::read(path).map_err(|source| BenchError::ReadFile {
        path: path.to_path_buf(),
        source,
    })?;
    let trace: Vec<TraceOp> =
        serde_json::from_slice(&bytes).map_err(|source| BenchError::ParseJson {
            path: path.to_path_buf(),
            source,
        })?;
    validate_trace(&trace)?;
    Ok(trace)
}

pub fn validate_trace(trace: &[TraceOp]) -> BenchResult<()> {
    let mut last_ts = 0;
    for (index, op) in trace.iter().enumerate() {
        if !op.path.starts_with('/') {
            return trace_error(index, "path must be absolute");
        }
        if op.path.contains("//") || op.path.contains("/../") || op.path.ends_with("/..") {
            return trace_error(index, "path must be normalized");
        }
        if index > 0 && op.timestamp_ns < last_ts {
            return trace_error(index, "timestamp_ns must be monotonic");
        }
        last_ts = op.timestamp_ns;

        match op.op {
            OpKind::Read => {
                if op.offset.is_none() || op.length.is_none() {
                    return trace_error(index, "read requires offset and length");
                }
            }
            OpKind::Write => {
                if op.offset.is_none() || op.length.is_none() || op.payload_seed.is_none() {
                    return trace_error(index, "write requires offset, length, and payload_seed");
                }
            }
            OpKind::Rename => {
                let Some(to_path) = &op.to_path else {
                    return trace_error(index, "rename requires to_path");
                };
                if !to_path.starts_with('/') {
                    return trace_error(index, "rename to_path must be absolute");
                }
            }
            OpKind::Getattr | OpKind::Lookup | OpKind::Readdir | OpKind::Delete | OpKind::Mkdir => {
            }
        }
    }
    Ok(())
}

fn trace_error(index: usize, message: &str) -> BenchResult<()> {
    Err(BenchError::TraceValidation {
        index,
        message: message.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn op(kind: OpKind, path: &str) -> TraceOp {
        TraceOp {
            timestamp_ns: 0,
            op: kind,
            path: path.to_string(),
            to_path: None,
            offset: None,
            length: None,
            payload_seed: None,
            parallel_group: None,
        }
    }

    #[test]
    fn validation_rejects_relative_path() {
        let trace = vec![op(OpKind::Getattr, "relative.txt")];
        let err = validate_trace(&trace).expect_err("relative paths must fail validation");
        assert!(err.to_string().contains("path must be absolute"));
    }

    #[test]
    fn validation_rejects_non_monotonic_timestamps() {
        let mut first = op(OpKind::Getattr, "/a");
        first.timestamp_ns = 10;
        let mut second = op(OpKind::Lookup, "/b");
        second.timestamp_ns = 9;
        let err = validate_trace(&[first, second]).expect_err("timestamps must be monotonic");
        assert!(err.to_string().contains("timestamp_ns must be monotonic"));
    }

    #[test]
    fn validation_requires_read_range() {
        let err =
            validate_trace(&[op(OpKind::Read, "/file")]).expect_err("read without range must fail");
        assert!(err.to_string().contains("read requires offset and length"));
    }

    #[test]
    fn validation_requires_write_seed() {
        let mut write = op(OpKind::Write, "/file");
        write.offset = Some(0);
        write.length = Some(32);
        let err = validate_trace(&[write]).expect_err("write without seed must fail");
        assert!(err
            .to_string()
            .contains("write requires offset, length, and payload_seed"));
    }

    #[test]
    fn validation_accepts_complete_trace() {
        let mut read = op(OpKind::Read, "/file");
        read.offset = Some(0);
        read.length = Some(64);
        let mut write = op(OpKind::Write, "/file");
        write.timestamp_ns = 1;
        write.offset = Some(64);
        write.length = Some(32);
        write.payload_seed = Some(7);
        validate_trace(&[read, write]).expect("complete trace should validate");
    }

    #[test]
    fn json_uses_lowercase_operation_names() {
        let raw = r#"[{"timestamp_ns":0,"op":"readdir","path":"/workspace"}]"#;
        let trace: Vec<TraceOp> = serde_json::from_str(raw).expect("trace json should parse");
        assert_eq!(trace[0].op, OpKind::Readdir);
    }
}
