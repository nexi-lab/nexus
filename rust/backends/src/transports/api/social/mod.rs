//! Social / feed connectors — Slack, X (Twitter), Hacker News, Nostr.

pub mod hn;
#[cfg(feature = "nostr")]
pub mod nostr;
pub mod slack;
pub mod x;
