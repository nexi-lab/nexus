//! Auth surface — abstracts the AuthService contract the adapter
//! consumes so the same router composes against (a) the production
//! AuthService (D2-onward, when the Rust shim lands) and (b) a stub
//! backend for tests.
//!
//! The adapter stays the only place that translates between Matrix
//! login JSON / bearer tokens and the kernel's `OperationContext`
//! identity stamp. Every other endpoint reads `OperationContext` from
//! request extensions populated by the access-token middleware.

use std::sync::Arc;

use async_trait::async_trait;

/// Resolved Matrix session — what `login` returns and what the
/// access-token middleware looks up on every request.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AuthSession {
    /// Matrix-formatted user id (`@local-part:server-name`). Today
    /// `local-part` matches nexus's `OperationContext.user_id`.
    pub user_id: String,
    /// Opaque bearer token Matrix clients send back as
    /// `Authorization: Bearer ...`. Mirrors the AuthService session
    /// token; stable for the lifetime of the session.
    pub access_token: String,
    /// Matrix `device_id` echoed back to the client. Today nexus
    /// assigns one device per token; the adapter keeps the value
    /// opaque.
    pub device_id: String,
}

/// Adapter's view of the AuthService surface. D1 wires a stub impl
/// for tests; D2 onwards swaps in a Rust shim that delegates to the
/// real AuthService.
#[async_trait]
pub trait AuthBackend: Send + Sync + 'static {
    /// Validate `m.login.password` credentials and return the resolved
    /// session, or `Err(AuthError::Forbidden)` on rejection. Other
    /// flows (`m.login.token`, SSO) come later — the trait is
    /// extensible without breaking call sites.
    async fn login_password(
        &self,
        identifier: &str,
        password: &str,
    ) -> Result<AuthSession, AuthError>;

    /// Resolve a bearer access token to an `AuthSession`. Returns
    /// `Err(AuthError::UnknownToken)` for tokens the backend doesn't
    /// recognise.
    async fn resolve_token(&self, token: &str) -> Result<AuthSession, AuthError>;

    /// Invalidate a session token. Idempotent: logging out an unknown
    /// token returns `Ok(())` so logout is safe to retry.
    async fn logout(&self, token: &str) -> Result<(), AuthError>;
}

/// Auth-side error. The adapter HTTP layer translates these into the
/// matching `AdapterError` (`M_FORBIDDEN`, `M_UNKNOWN_TOKEN`, …) when
/// surfacing to the client.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthError {
    Forbidden(String),
    UnknownToken,
    Backend(String),
}

impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Forbidden(m) => write!(f, "auth forbidden: {m}"),
            Self::UnknownToken => write!(f, "auth: unknown token"),
            Self::Backend(m) => write!(f, "auth backend: {m}"),
        }
    }
}

impl std::error::Error for AuthError {}

/// Convenience type — the adapter passes around `Arc<dyn AuthBackend>`
/// so multiple handlers can share a single backend without lifetime
/// gymnastics.
pub type AuthBackendRef = Arc<dyn AuthBackend>;

#[cfg(test)]
pub(crate) mod stub {
    //! In-memory stub backend used by adapter integration tests. Not
    //! wired into production builds.

    use super::*;
    use parking_lot::RwLock;
    use std::collections::HashMap;

    /// Toy auth backend — accepts a fixed set of `(user, password)`
    /// pairs and mints deterministic tokens. Tests pre-seed the map
    /// with the credentials they want accepted.
    pub struct StubAuthBackend {
        passwords: RwLock<HashMap<String, String>>, // user → password
        tokens: RwLock<HashMap<String, AuthSession>>, // token → session
        server_name: String,
    }

    impl StubAuthBackend {
        pub fn new(server_name: impl Into<String>) -> Self {
            Self {
                passwords: RwLock::new(HashMap::new()),
                tokens: RwLock::new(HashMap::new()),
                server_name: server_name.into(),
            }
        }

        pub fn add_user(&self, user: &str, password: &str) {
            self.passwords
                .write()
                .insert(user.to_string(), password.to_string());
        }
    }

    #[async_trait]
    impl AuthBackend for StubAuthBackend {
        async fn login_password(
            &self,
            identifier: &str,
            password: &str,
        ) -> Result<AuthSession, AuthError> {
            let stored = self
                .passwords
                .read()
                .get(identifier)
                .cloned()
                .ok_or_else(|| AuthError::Forbidden(format!("unknown user {identifier:?}")))?;
            if stored != password {
                return Err(AuthError::Forbidden("password mismatch".into()));
            }
            let session = AuthSession {
                user_id: format!("@{}:{}", identifier, self.server_name),
                access_token: format!("stub-token-{identifier}"),
                device_id: format!("stub-device-{identifier}"),
            };
            self.tokens
                .write()
                .insert(session.access_token.clone(), session.clone());
            Ok(session)
        }

        async fn resolve_token(&self, token: &str) -> Result<AuthSession, AuthError> {
            self.tokens
                .read()
                .get(token)
                .cloned()
                .ok_or(AuthError::UnknownToken)
        }

        async fn logout(&self, token: &str) -> Result<(), AuthError> {
            self.tokens.write().remove(token);
            Ok(())
        }
    }
}
