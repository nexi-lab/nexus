//! Shared blocking HTTP client for the plugin's provider calls —
//! remote embedder, LLM query expansion, contextual chunking —
//! hardened for #4725.
//!
//! # Why this exists
//!
//! `reqwest::blocking::Client` drives every request on a private
//! current-thread tokio runtime.  Two properties of that design bit
//! the cluster host under a cgroup `pids.max`:
//!
//! 1. hyper-util's default DNS resolver runs `getaddrinfo` through
//!    `tokio::task::spawn_blocking` on that private runtime.  Its
//!    blocking threads exit after 10 s idle, so every lookup after a
//!    quiet spell spawns a fresh OS thread — and when the OS refuses
//!    (`EAGAIN`) while the pool is empty, tokio PANICS inside the
//!    resolver (`OS can't spawn worker thread`).  [`InlineDnsResolver`]
//!    resolves synchronously on the runtime thread instead: zero
//!    threads per lookup, and that panic site is gone.  The cost is
//!    that a slow `getaddrinfo` stalls the client's other in-flight
//!    requests for its duration; hyper's connection pool makes lookups
//!    rare (one per NEW connection, not per request) and the plugin's
//!    provider fan-out is a handful of calls, so the trade is right
//!    here.
//! 2. When the private runtime drops a request, the blocking client
//!    PANICS on the caller's thread (`event loop thread panicked`)
//!    instead of returning an error — and if that runtime really died,
//!    every later call panics too.  [`GuardedClient::send`] catches the
//!    panic, reports it as an ordinary [`SendError`], and rebuilds the
//!    client so the next call gets a live runtime.
//!
//! One [`GuardedClient`] per feature, built once and reused: building
//! a blocking client spawns its runtime thread, so per-call
//! construction would itself be the thread churn #4725 is about.

use std::any::Any;
use std::net::ToSocketAddrs;
use std::sync::Arc;
use std::time::Duration;

use parking_lot::Mutex;

/// DNS resolution on the calling runtime thread via the OS resolver
/// (`getaddrinfo`) — no `spawn_blocking`, no thread per lookup.  See
/// the module doc for the trade-off.
pub struct InlineDnsResolver;

impl reqwest::dns::Resolve for InlineDnsResolver {
    fn resolve(&self, name: reqwest::dns::Name) -> reqwest::dns::Resolving {
        let host = name.as_str().to_string();
        Box::pin(async move {
            // Port 0: reqwest overrides it with the URL's port or the
            // scheme default.
            (host.as_str(), 0u16)
                .to_socket_addrs()
                .map(|addrs| Box::new(addrs) as reqwest::dns::Addrs)
                .map_err(|e| Box::new(e) as Box<dyn std::error::Error + Send + Sync>)
        })
    }
}

/// Why a guarded send failed.
#[derive(Debug, thiserror::Error)]
pub enum SendError {
    /// Ordinary transport / status error from reqwest.
    #[error("{0}")]
    Http(#[from] reqwest::Error),
    /// The request panicked inside reqwest — its private runtime
    /// dropped the request.  The client has already been rebuilt;
    /// the caller retries on its own cadence.
    #[error("request aborted inside the HTTP client ({0}); client rebuilt — retry")]
    Panicked(String),
}

/// A long-lived blocking client that resolves DNS inline and survives
/// a request panicking inside reqwest.  The `Arc`-backed `Client`
/// sits behind a mutex only so [`Self::send`] can swap it atomically
/// after a panic; callers hold the lock for a clone.
pub struct GuardedClient {
    client: Mutex<reqwest::blocking::Client>,
    timeout: Duration,
    #[cfg(test)]
    panic_next_send: std::sync::atomic::AtomicBool,
}

impl GuardedClient {
    /// Build the client.  No network I/O — a bad endpoint surfaces on
    /// the first send.
    pub fn new(timeout: Duration) -> Result<Self, reqwest::Error> {
        Ok(Self {
            client: Mutex::new(Self::build(timeout)?),
            timeout,
            #[cfg(test)]
            panic_next_send: std::sync::atomic::AtomicBool::new(false),
        })
    }

    fn build(timeout: Duration) -> Result<reqwest::blocking::Client, reqwest::Error> {
        reqwest::blocking::Client::builder()
            .timeout(timeout)
            .dns_resolver(Arc::new(InlineDnsResolver))
            .build()
    }

    /// Start a POST.  The returned builder carries its own handle to
    /// the client, so the lock is released before the caller adds
    /// headers / body.
    pub fn post(&self, url: &str) -> reqwest::blocking::RequestBuilder {
        self.client.lock().clone().post(url)
    }

    /// Send, converting a panic inside reqwest into
    /// [`SendError::Panicked`] and rebuilding the client (#4725).
    pub fn send(
        &self,
        req: reqwest::blocking::RequestBuilder,
    ) -> Result<reqwest::blocking::Response, SendError> {
        #[cfg(test)]
        let inject_panic = self
            .panic_next_send
            .swap(false, std::sync::atomic::Ordering::SeqCst);
        let sent = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
            #[cfg(test)]
            if inject_panic {
                panic!("injected HTTP client panic");
            }
            req.send()
        }));
        match sent {
            Ok(result) => Ok(result?),
            Err(payload) => {
                let reason = panic_reason(payload.as_ref());
                tracing::warn!(
                    reason = %reason,
                    "HTTP client: request panicked inside reqwest — rebuilding the client",
                );
                self.rebuild();
                Err(SendError::Panicked(reason))
            }
        }
    }

    /// Best-effort replacement after a panic.  If the rebuild itself
    /// fails (still under thread pressure — building spawns the
    /// runtime thread) the existing client stays; the panic may well
    /// have been per-request with the runtime still alive.
    fn rebuild(&self) {
        match Self::build(self.timeout) {
            Ok(fresh) => *self.client.lock() = fresh,
            Err(e) => tracing::warn!(
                err = %e,
                "HTTP client: rebuild failed — keeping the existing client",
            ),
        }
    }

    /// Test seam: make the next [`Self::send`] panic inside the
    /// guarded region.
    #[cfg(test)]
    pub(crate) fn inject_panic_on_next_send(&self) {
        self.panic_next_send
            .store(true, std::sync::atomic::Ordering::SeqCst);
    }
}

/// Human-readable panic payload — `panic!` with a literal yields
/// `&str`, with a format string `String`.  Shared by the HTTP guard
/// and the plugin's dispatch-boundary guard (`lib.rs`).
pub(crate) fn panic_reason(payload: &(dyn Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "non-string panic payload".to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::llm_chat::test_http::spawn_one_shot;

    #[test]
    fn inline_resolver_resolves_localhost() {
        use reqwest::dns::Resolve;
        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .expect("rt");
        let Ok(name) = "localhost".parse::<reqwest::dns::Name>() else {
            panic!("localhost is a valid name");
        };
        let addrs: Vec<_> = rt
            .block_on(InlineDnsResolver.resolve(name))
            .expect("resolve")
            .collect();
        assert!(!addrs.is_empty());
        assert!(addrs.iter().all(|a| a.ip().is_loopback()), "{addrs:?}");
    }

    #[test]
    fn guarded_send_reports_panic_and_keeps_working() {
        let client = GuardedClient::new(Duration::from_secs(5)).expect("client");
        client.inject_panic_on_next_send();
        let err = client
            .send(client.post("http://127.0.0.1:9/never-sent"))
            .expect_err("injected panic must surface as an error");
        assert!(
            matches!(err, SendError::Panicked(ref r) if r.contains("injected")),
            "{err}"
        );

        // The rebuilt client serves a real request.
        let (addr, server) = spawn_one_shot("200 OK", r#"{"ok":true}"#.to_string());
        let resp = client
            .send(client.post(&format!("http://{addr}/ping")).body("x"))
            .expect("send after rebuild");
        assert!(resp.status().is_success());
        let request = server.join().expect("server thread");
        assert!(request.starts_with("POST /ping"), "{request}");
    }
}
