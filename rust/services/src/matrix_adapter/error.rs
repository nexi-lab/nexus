//! Adapter-wide error type. Surfaces as Matrix-spec error JSON via the
//! axum `IntoResponse` impl.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use serde_json::json;

/// Adapter-level error. Each variant maps to a Matrix `errcode` (the
/// stable string Matrix clients dispatch on) plus the HTTP status the
/// spec mandates for that error.
#[derive(Debug, Clone)]
pub enum AdapterError {
    /// `M_FORBIDDEN` — credentials were missing or rejected.
    Forbidden(String),
    /// `M_UNKNOWN_TOKEN` — bearer token did not resolve to a session.
    UnknownToken,
    /// `M_MISSING_TOKEN` — request reached a token-protected endpoint
    /// without an `Authorization` header.
    MissingToken,
    /// `M_BAD_JSON` — request body failed to parse.
    BadJson(String),
    /// `M_UNRECOGNIZED` — endpoint exists but the requested
    /// login type / flow is not implemented yet.
    Unrecognized(String),
    /// `M_UNKNOWN` — anything else; the message goes into the response.
    Internal(String),
}

impl AdapterError {
    fn errcode(&self) -> &'static str {
        match self {
            Self::Forbidden(_) => "M_FORBIDDEN",
            Self::UnknownToken => "M_UNKNOWN_TOKEN",
            Self::MissingToken => "M_MISSING_TOKEN",
            Self::BadJson(_) => "M_BAD_JSON",
            Self::Unrecognized(_) => "M_UNRECOGNIZED",
            Self::Internal(_) => "M_UNKNOWN",
        }
    }

    fn status(&self) -> StatusCode {
        match self {
            Self::Forbidden(_) => StatusCode::FORBIDDEN,
            Self::UnknownToken | Self::MissingToken => StatusCode::UNAUTHORIZED,
            Self::BadJson(_) | Self::Unrecognized(_) => StatusCode::BAD_REQUEST,
            Self::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    fn error_message(&self) -> String {
        match self {
            Self::Forbidden(m) | Self::BadJson(m) | Self::Unrecognized(m) | Self::Internal(m) => {
                m.clone()
            }
            Self::UnknownToken => "access token unknown".to_string(),
            Self::MissingToken => "missing access token".to_string(),
        }
    }
}

impl std::fmt::Display for AdapterError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.errcode(), self.error_message())
    }
}

impl std::error::Error for AdapterError {}

impl IntoResponse for AdapterError {
    fn into_response(self) -> Response {
        let body = json!({
            "errcode": self.errcode(),
            "error": self.error_message(),
        });
        (self.status(), Json(body)).into_response()
    }
}
