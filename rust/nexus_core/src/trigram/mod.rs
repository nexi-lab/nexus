//! Memory-mapped trigram index for sub-20ms grep on 100K+ files.
//!
//! This module implements a trigram-based inverted index inspired by
//! [Google Code Search](https://swtch.com/~rsc/regexp/regexp4.html) and
//! [Zoekt](https://github.com/sourcegraph/zoekt).
//!
//! # Architecture
//!
//! - **extract** — Trigram extraction from byte content
//! - **query** — Build trigram queries from patterns and regex
//! - **posting** — Posting list operations using Roaring bitmaps
//! - **format** — Binary index format with CRC32 integrity checks
//! - **builder** — In-memory index construction
//! - **writer** — Serialize index to bytes (WASM-safe, no file I/O)
//! - **error** — Error types
//!
//! The I/O layer (mmap, file read/write) lives in `nexus_pyo3::trigram`.

pub mod builder;
pub mod error;
pub mod extract;
pub mod format;
pub mod posting;
pub mod query;
pub mod writer;

// Re-export key types for convenience.
pub use builder::TrigramIndexBuilder;
pub use error::TrigramError;
pub use query::{build_trigram_query, TrigramQuery};
pub use writer::write_index;
