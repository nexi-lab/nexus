//! Matrix Client-Server v3 request / response shapes for the auth
//! surface. Field names match the spec verbatim — Matrix clients
//! dispatch on JSON keys, not Rust types, so any rename breaks the
//! contract.

use serde::{Deserialize, Serialize};

/// `POST /_matrix/client/v3/login` request body.
///
/// Today the adapter accepts the `m.login.password` flow only; other
/// flows return `M_UNRECOGNIZED` so clients downgrade gracefully. The
/// `identifier` shape matches the spec's `UserIdentifier` object —
/// only the `m.id.user` variant is honoured at D1.
#[derive(Debug, Clone, Deserialize)]
pub struct LoginRequest {
    #[serde(rename = "type")]
    pub login_type: String,
    pub identifier: UserIdentifier,
    pub password: Option<String>,
    /// Matrix lets clients pin a device id across reconnects so push
    /// notifications survive token rotation. Optional at D1.
    pub device_id: Option<String>,
    pub initial_device_display_name: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct UserIdentifier {
    #[serde(rename = "type")]
    pub id_type: String,
    pub user: Option<String>,
}

/// `POST /_matrix/client/v3/login` response.
#[derive(Debug, Clone, Serialize)]
pub struct LoginResponse {
    pub user_id: String,
    pub access_token: String,
    pub device_id: String,
    pub home_server: String,
}

/// `POST /_matrix/client/v3/logout` response. Matrix returns an empty
/// JSON object; serialising a unit struct with `serde_json` produces
/// `null`, so use a dedicated empty struct.
#[derive(Debug, Clone, Serialize, Default)]
pub struct EmptyResponse {}

/// `GET /_matrix/client/v3/account/whoami` response.
#[derive(Debug, Clone, Serialize)]
pub struct WhoAmIResponse {
    pub user_id: String,
    pub device_id: String,
    /// Matrix spec optional flag — the adapter never issues guest
    /// tokens, so it's always `false`. Required by some clients.
    pub is_guest: bool,
}
