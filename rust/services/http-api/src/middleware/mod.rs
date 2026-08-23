//! Tower middleware wired above [`crate::router`].
//!
//! Kept in a sibling module to `handlers/` so a new middleware layer
//! (auth, rate-limit, request-id, tracing) does not touch handler
//! code — layers compose additively at `router()` assembly time.

pub mod auth;
