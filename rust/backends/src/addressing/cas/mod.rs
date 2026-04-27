//! CAS addressing — content-hash-keyed blob storage.
//!
//! Phase 2 keeps the CAS engine + chunking + remote fetcher + local
//! transport **in the kernel crate** (`kernel::cas_engine`,
//! `kernel::cas_chunking`, `kernel::cas_remote`,
//! `kernel::cas_transport`) because they're the kernel's
//! content-addressed storage primitive — the Linux-VFS-equivalent
//! pillar that backends consume rather than implement.
//!
//! This module is a placeholder for future CAS-side helpers that
//! belong to backends (e.g. per-backend CAS sidecar metadata, gc
//! policy hooks).  Today it's empty; the `addressing/cas/` directory
//! exists so the file layout reflects the architecture's
//! `backends::addressing::cas::*` shape even when the implementation
//! gravitates kernel-side for primitive sharing.
