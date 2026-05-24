//! `transport::auth` — `AuthProvider` trait + the kernel-default
//! `NoAuth` impl.
//!
//! ## Why this lives in `transport`, not `services`
//!
//! The `AuthProvider` trait is consumed by
//! `transport::grpc::VfsServiceImpl` to gate token-bearing requests
//! before they reach the kernel. By the Rust convention of "trait
//! owner = primary consumer", the trait belongs to `transport`.
//! `NoAuth` ships alongside the trait because it is the
//! kernel-default all-pass policy: `nexusd-cluster` runs it directly
//! (mTLS is the boundary), so cluster has no reason to pull a
//! services-tier crate just to find it.
//!
//! ## Where future auth impls go
//!
//! Anything beyond kernel-default all-pass (API-key gateways, JWT,
//! OIDC, mTLS-claim mapping, …) is a property of the deployment-tier
//! service that introduces it. Those impls live in `services/<their
//! folder>/auth.rs` and `impl transport::auth::AuthProvider for …`
//! through the workspace orphan-rule relaxation. They DO NOT
//! retroactively belong here.

use kernel::kernel::OperationContext;

/// Resolve a bearer token into an `OperationContext`.
pub trait AuthProvider: Send + Sync + 'static {
    fn resolve(&self, token: &str) -> Result<OperationContext, tonic::Status>;
}

/// Kernel-default all-pass policy. Every request becomes a
/// system-level admin context regardless of the supplied token.
/// `nexusd-cluster` uses this directly — mTLS is the boundary.
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

#[cfg(test)]
mod tests {
    use super::*;

    /// Reject helper used in tests to exercise the rejection branch
    /// of consumer code without dragging a real auth backend into the
    /// transport crate.
    struct RejectAll;
    impl AuthProvider for RejectAll {
        fn resolve(&self, _token: &str) -> Result<OperationContext, tonic::Status> {
            Err(tonic::Status::unauthenticated("rejected"))
        }
    }

    #[test]
    fn no_auth_returns_admin_context_for_any_token() {
        let auth = NoAuth;
        for token in ["", "any-token-here", "x"] {
            let ctx = auth.resolve(token).unwrap();
            assert_eq!(ctx.user_id, "cluster-internal");
            assert!(ctx.is_admin);
            assert!(ctx.is_system);
        }
    }

    #[test]
    fn reject_all_helper_rejects() {
        assert!(RejectAll.resolve("anything").is_err());
    }
}
