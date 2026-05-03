//! Matrix Client-Server v3 adapter — exposes nexus chat-with-me
//! DT_STREAMs as Matrix rooms so stock chat clients (Element,
//! FluffyChat, Cinny) participate in nexus conversations without a
//! bespoke client. End-state spec lives in
//! `sudowork-2/docs/tech/nexus-integration-architecture.md` §4.2.
//!
//! D1 lands the skeleton + auth surface:
//!
//!   * `axum::Router` exposing `/_matrix/client/v3/login`,
//!     `/_matrix/client/v3/logout`, `/_matrix/client/v3/account/whoami`.
//!   * `AuthBackend` trait abstracting credential verification + token
//!     resolution so the same router composes against the production
//!     `AuthService` (D2-onward) and a stub backend for tests.
//!   * Access-token middleware that turns `Authorization: Bearer ...`
//!     into an `OperationContext` extension on the request, ready for
//!     D2's room read/write handlers to stamp into kernel syscalls.
//!
//! The adapter is stateless: the kernel's metastore + WalStreamBackend
//! hold the SSOT, so the only in-process state the adapter keeps is
//! `/sync` long-poll registrations + an access-token cache (D3
//! responsibility).

pub mod auth;
pub mod error;
pub mod middleware;
pub mod pdu;
pub mod room_id;
pub mod router;
pub mod types;

pub use auth::{AuthBackend, AuthError, AuthSession};
pub use error::AdapterError;
pub use pdu::{chat_envelope_to_pdu_event, pdu_send_to_chat_envelope};
pub use room_id::{decode_room_id, encode_room_id};
pub use router::{build_router, AdapterState};
