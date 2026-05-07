use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

use crate::{
    error::{BenchError, BenchResult},
    metrics::OperationMetrics,
    trace::{OpKind, TraceOp},
};

use super::BenchTarget;

#[derive(Debug, Clone)]
pub struct MountTarget {
    root: PathBuf,
}

impl MountTarget {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    fn resolve(&self, path: &str) -> PathBuf {
        let relative = path.trim_start_matches('/');
        self.root.join(relative)
    }
}

impl BenchTarget for MountTarget {
    fn name(&self) -> &'static str {
        "mount"
    }

    fn execute(&self, op: &TraceOp) -> BenchResult<OperationMetrics> {
        let path = self.resolve(&op.path);
        match op.op {
            OpKind::Read => read_range(&path, op),
            OpKind::Write => write_range(&path, op),
            OpKind::Getattr | OpKind::Lookup => {
                fs::metadata(&path).map_err(|source| BenchError::Io { path, source })?;
                Ok(metrics(0, 0, 1, 0))
            }
            OpKind::Readdir => {
                let count = fs::read_dir(&path)
                    .map_err(|source| BenchError::Io {
                        path: path.clone(),
                        source,
                    })?
                    .count();
                Ok(metrics(0, 0, 1, count as u64))
            }
            OpKind::Delete => {
                remove_path(&path)?;
                Ok(metrics(0, 0, 1, 0))
            }
            OpKind::Rename => {
                let to_path = self.resolve(op.to_path.as_deref().unwrap_or("/"));
                fs::rename(&path, &to_path).map_err(|source| BenchError::Io { path, source })?;
                Ok(metrics(0, 0, 1, 0))
            }
            OpKind::Mkdir => {
                fs::create_dir_all(&path).map_err(|source| BenchError::Io { path, source })?;
                Ok(metrics(0, 0, 1, 0))
            }
        }
    }
}

fn read_range(path: &Path, op: &TraceOp) -> BenchResult<OperationMetrics> {
    let offset = op.offset.unwrap_or(0);
    let len = op.length.unwrap_or(0) as usize;
    let mut file = File::open(path).map_err(|source| BenchError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    file.seek(SeekFrom::Start(offset))
        .map_err(|source| BenchError::Io {
            path: path.to_path_buf(),
            source,
        })?;
    let mut buf = vec![0; len];
    let mut read = 0;
    while read < len {
        let count = file
            .read(&mut buf[read..])
            .map_err(|source| BenchError::Io {
                path: path.to_path_buf(),
                source,
            })?;
        if count == 0 {
            return Err(BenchError::Target(format!(
                "short read from {}: expected {} bytes, got {} bytes",
                path.display(),
                len,
                read
            )));
        }
        read += count;
    }
    Ok(metrics(len as u64, 0, 1, len as u64))
}

fn write_range(path: &Path, op: &TraceOp) -> BenchResult<OperationMetrics> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|source| BenchError::Io {
            path: parent.to_path_buf(),
            source,
        })?;
    }
    let offset = op.offset.unwrap_or(0);
    let len = op.length.unwrap_or(0) as usize;
    let mut file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(path)
        .map_err(|source| BenchError::Io {
            path: path.to_path_buf(),
            source,
        })?;
    file.seek(SeekFrom::Start(offset))
        .map_err(|source| BenchError::Io {
            path: path.to_path_buf(),
            source,
        })?;
    let payload = seeded_payload(op.payload_seed.unwrap_or(0), len);
    file.write_all(&payload).map_err(|source| BenchError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    Ok(metrics(0, len as u64, 1, 0))
}

fn remove_path(path: &Path) -> BenchResult<()> {
    let metadata = fs::metadata(path).map_err(|source| BenchError::Io {
        path: path.to_path_buf(),
        source,
    })?;
    if metadata.is_dir() {
        fs::remove_dir_all(path).map_err(|source| BenchError::Io {
            path: path.to_path_buf(),
            source,
        })
    } else {
        fs::remove_file(path).map_err(|source| BenchError::Io {
            path: path.to_path_buf(),
            source,
        })
    }
}

fn seeded_payload(seed: u64, len: usize) -> Vec<u8> {
    (0..len)
        .map(|idx| seed.wrapping_add(idx as u64) as u8)
        .collect()
}

fn metrics(read: u64, written: u64, rpc_count: u64, egress: u64) -> OperationMetrics {
    OperationMetrics {
        logical_bytes_read: read,
        logical_bytes_written: written,
        rpc_count,
        egress_bytes: egress,
        cache_hit: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::trace::{OpKind, TraceOp};

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
    fn mount_target_reads_requested_range_from_root() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("file.bin"), b"abcdef").unwrap();
        let target = MountTarget::new(temp.path().to_path_buf());
        let mut read = op(OpKind::Read, "/file.bin");
        read.offset = Some(2);
        read.length = Some(3);

        let metrics = target.execute(&read).expect("range read should succeed");
        assert_eq!(metrics.logical_bytes_read, 3);
        assert_eq!(metrics.egress_bytes, 3);
    }

    #[test]
    fn mount_target_rejects_short_reads() {
        let temp = tempfile::tempdir().unwrap();
        std::fs::write(temp.path().join("file.bin"), b"ab").unwrap();
        let target = MountTarget::new(temp.path().to_path_buf());
        let mut read = op(OpKind::Read, "/file.bin");
        read.offset = Some(0);
        read.length = Some(4);

        let err = target
            .execute(&read)
            .expect_err("short read should fail the benchmark operation");

        assert!(err.to_string().contains("short read"));
        assert!(err.to_string().contains("expected 4 bytes"));
    }

    #[test]
    fn mount_target_writes_seeded_payload() {
        let temp = tempfile::tempdir().unwrap();
        let target = MountTarget::new(temp.path().to_path_buf());
        let mut write = op(OpKind::Write, "/out.bin");
        write.offset = Some(0);
        write.length = Some(4);
        write.payload_seed = Some(9);

        let metrics = target.execute(&write).expect("write should succeed");
        assert_eq!(metrics.logical_bytes_written, 4);
        assert_eq!(std::fs::read(temp.path().join("out.bin")).unwrap().len(), 4);
    }
}
