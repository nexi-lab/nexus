use std::path::PathBuf;

pub type BenchResult<T> = Result<T, BenchError>;

#[derive(Debug, thiserror::Error)]
pub enum BenchError {
    #[error("trace validation failed at operation {index}: {message}")]
    TraceValidation { index: usize, message: String },

    #[error("failed to read {path}: {source}")]
    ReadFile {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("failed to parse json from {path}: {source}")]
    ParseJson {
        path: PathBuf,
        #[source]
        source: serde_json::Error,
    },

    #[error("io error at {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },

    #[error("target operation failed: {0}")]
    Target(String),

    #[error("http request failed: {0}")]
    Http(String),

    #[error("diff threshold failed: {0}")]
    Threshold(String),
}
