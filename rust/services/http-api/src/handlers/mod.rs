//! Route handler modules — one file per `/v2/*` route domain.
//!
//! Each submodule exports a `pub fn router() -> axum::Router<AppState>`
//! that `crate::router` merges into the aggregate.  A handler NEVER
//! wires state directly; it takes `axum::extract::State<AppState>`
//! and pulls whichever backend it needs off the state struct.  This
//! keeps `AppState` growth O(1) per new domain — a fresh domain
//! adds a field, no handler rewire.

pub mod documents;
pub mod search;
pub mod status;
