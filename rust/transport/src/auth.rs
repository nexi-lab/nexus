//! `transport::auth` — Rust-native authentication providers.
//!
//! | Provider     | Use case                           |
//! |--------------|------------------------------------|
//! | `ApiKeyAuth` | Static API key (HMAC-CT compare)   |
//! | `NoAuth`     | Cluster-internal (no token needed) |
//!
//! The trait is consumed by `transport::grpc::VfsServiceImpl` via
//! `Arc<dyn AuthProvider>`, so the gRPC server has zero PyO3 coupling.
//! Lives in `transport` (not `services`) because auth is a property of
//! the network surface: it sits between the wire and the kernel, gates
//! token-bearing requests, and is consumed only by the gRPC server. The
//! `services` crate is post-syscall hooks (audit / agents / tasks) and
//! has no reason to know about bearer tokens.

use std::sync::Arc;

use kernel::kernel::OperationContext;

// ── Trait ───────────────────────────────────────────────────────────

/// Resolve a bearer token into an `OperationContext`.
pub trait AuthProvider: Send + Sync + 'static {
    fn resolve(&self, token: &str) -> Result<OperationContext, tonic::Status>;
}

/// Convenience alias.
pub type AuthProviderRef = Arc<dyn AuthProvider>;

// ── ApiKeyAuth ──────────────────────────────────────────────────────

/// Constant-time HMAC comparison against a static API key.
pub struct ApiKeyAuth {
    expected: Arc<str>,
}

impl ApiKeyAuth {
    pub fn new(key: impl Into<Arc<str>>) -> Self {
        Self {
            expected: key.into(),
        }
    }
}

impl AuthProvider for ApiKeyAuth {
    fn resolve(&self, token: &str) -> Result<OperationContext, tonic::Status> {
        if token.is_empty() {
            return Err(tonic::Status::unauthenticated("Authentication required"));
        }
        if subtle_eq(self.expected.as_bytes(), token.as_bytes()) {
            Ok(OperationContext::new(
                "api-key-user",
                "root",
                true,
                None,
                false,
            ))
        } else {
            Err(tonic::Status::unauthenticated("Invalid API key"))
        }
    }
}

// ── NoAuth ──────────────────────────────────────────────────────────

/// Cluster-internal: every request is treated as admin with no token
/// validation. Used by `nexusd-cluster` where mTLS is the only
/// authentication boundary.
pub struct NoAuth;

impl AuthProvider for NoAuth {
    fn resolve(&self, _token: &str) -> Result<OperationContext, tonic::Status> {
        Ok(OperationContext::new(
            "cluster-internal",
            "root",
            true,
            None,
            true,
        ))
    }
}

// ── Helpers ─────────────────────────────────────────────────────────

/// Constant-time byte equality.
fn subtle_eq(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    let mut diff = 0u8;
    for (x, y) in a.iter().zip(b.iter()) {
        diff |= x ^ y;
    }
    diff == 0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn api_key_auth_accepts_matching_key() {
        let auth = ApiKeyAuth::new("test-key-123");
        let ctx = auth.resolve("test-key-123").unwrap();
        assert_eq!(ctx.user_id, "api-key-user");
        assert!(ctx.is_admin);
    }

    #[test]
    fn api_key_auth_rejects_wrong_key() {
        let auth = ApiKeyAuth::new("test-key-123");
        assert!(auth.resolve("wrong-key").is_err());
    }

    #[test]
    fn api_key_auth_rejects_empty_token() {
        let auth = ApiKeyAuth::new("test-key-123");
        assert!(auth.resolve("").is_err());
    }

    #[test]
    fn no_auth_always_succeeds() {
        let auth = NoAuth;
        let ctx = auth.resolve("").unwrap();
        assert_eq!(ctx.user_id, "cluster-internal");
        assert!(ctx.is_admin);
        assert!(ctx.is_system);
    }

    #[test]
    fn no_auth_ignores_any_token() {
        let auth = NoAuth;
        let ctx = auth.resolve("any-token-here").unwrap();
        assert_eq!(ctx.user_id, "cluster-internal");
    }
}
