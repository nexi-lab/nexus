//! Error types for the trigram index.

use std::fmt;
use std::path::PathBuf;

/// Errors that can occur during trigram index operations.
#[derive(Debug)]
pub enum TrigramError {
    /// I/O error during index read/write.
    Io(std::io::Error),
    /// Index file data is corrupted.
    CorruptIndex { reason: String },
    /// Invalid magic bytes in index header.
    InvalidMagic,
    /// Index version does not match expected version.
    VersionMismatch { expected: u32, found: u32 },
    /// Invalid search pattern.
    InvalidPattern(String),
    /// Index file not found at expected path.
    IndexNotFound(PathBuf),
}

impl fmt::Display for TrigramError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            TrigramError::Io(e) => write!(f, "I/O error: {}", e),
            TrigramError::CorruptIndex { reason } => {
                write!(f, "Corrupt index: {}", reason)
            }
            TrigramError::InvalidMagic => write!(f, "Invalid magic bytes in index header"),
            TrigramError::VersionMismatch { expected, found } => {
                write!(
                    f,
                    "Version mismatch: expected {}, found {}",
                    expected, found
                )
            }
            TrigramError::InvalidPattern(p) => write!(f, "Invalid pattern: {}", p),
            TrigramError::IndexNotFound(p) => {
                write!(f, "Index not found: {}", p.display())
            }
        }
    }
}

impl std::error::Error for TrigramError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            TrigramError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<std::io::Error> for TrigramError {
    fn from(e: std::io::Error) -> Self {
        TrigramError::Io(e)
    }
}
