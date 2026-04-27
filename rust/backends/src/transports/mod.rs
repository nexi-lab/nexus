//! Transports — `where` blobs travel to (local fs, cloud, external API).
//!
//! Two top-level kinds:
//!   * `blob` — Nexus-managed blob storage (local, S3, GCS).  These
//!     own the bytes; Nexus is the source of truth.
//!   * `api` — external API connectors (anthropic / openai / google /
//!     social / cli).  These connect to third-party services that
//!     own the bytes; Nexus mounts them as read-mostly views.

#[cfg(feature = "connectors")]
pub mod api;
pub mod blob;
