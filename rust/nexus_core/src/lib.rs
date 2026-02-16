//! `nexus_core` — portable Rust kernel for Nexus.
//!
//! This crate contains WASM-safe computation extracted from `nexus_fast`.
//! It compiles to `wasm32-unknown-unknown` and has zero CPython (PyO3) dependency.
//!
//! Modules:
//! - `types`  — domain types (Entity, Permission, etc.)
//! - `rebac`  — Relationship-Based Access Control engine
//! - `search` — line-oriented text search (literal + regex)
//! - `bloom`  — Bloom filter for fast set-membership checks
//! - `hash`   — BLAKE3 content hashing
//! - `glob`   — Glob pattern matching
//! - `bitmap` — Roaring Bitmap operations

pub mod bitmap;
pub mod bloom;
pub mod glob;
pub mod hash;
pub mod rebac;
pub mod search;
pub mod trigram;
pub mod types;
