//! Integration coverage for the `/v2/auth/keys` router.
//!
//! Real-chain shape: bind an ephemeral axum listener over an
//! `AppState` whose `auth_key_store` is a hand-rolled in-memory
//! double (populated with pre-encoded `AuthKeyRecord` bytes).
//! The list handler reads the store, decodes each row, filters by
//! query params, and shapes the response.  Revoke calls
//! `store.delete(hash)`.  Same wire shape the raft-backed store
//! serves in production.

use std::sync::Arc;

use auth::record::{AuthKeyRecord, SubjectType};
use contracts::operation_context::OperationContext;
use kernel::hal::auth_key_store::{AuthKeyStore, AuthKeyStoreError};
use nexus_http_api::{bind_and_serve, AppState};
use parking_lot::Mutex;
use serde_json::json;
use std::collections::BTreeMap;
use transport::auth::{AuthCredentials, AuthProvider};

// ── shared test doubles ──────────────────────────────────────────

/// Store double — records get + put + delete + list against a
/// `BTreeMap<String, Vec<u8>>`.  Matches the shape the mint layer's
/// own test double (`auth::mint::tests::MemStore`) uses.
#[derive(Default)]
struct MemStore {
    records: Mutex<BTreeMap<String, Vec<u8>>>,
}

impl AuthKeyStore for MemStore {
    fn get(&self, key_hash: &str) -> Result<Option<Vec<u8>>, AuthKeyStoreError> {
        Ok(self.records.lock().get(key_hash).cloned())
    }
    fn put(&self, key_hash: &str, record: &[u8]) -> Result<(), AuthKeyStoreError> {
        self.records
            .lock()
            .insert(key_hash.to_string(), record.to_vec());
        Ok(())
    }
    fn delete(&self, key_hash: &str) -> Result<bool, AuthKeyStoreError> {
        Ok(self.records.lock().remove(key_hash).is_some())
    }
    fn list(&self) -> Result<Vec<(String, Vec<u8>)>, AuthKeyStoreError> {
        Ok(self
            .records
            .lock()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect())
    }
}

/// AuthProvider that returns an admin OperationContext for the
/// bearer `"admin-token"`, a non-admin ctx for `"user-token"`, and
/// 401 for anything else.  Lets a single test file exercise both
/// the require_bearer middleware AND the /v2/auth/keys admin gate.
struct FixtureAuth;

impl AuthProvider for FixtureAuth {
    fn resolve(&self, creds: &AuthCredentials<'_>) -> Result<OperationContext, tonic::Status> {
        match creds.token {
            "admin-token" => Ok(OperationContext::new(
                "admin-user",
                "root",
                /* is_admin */ true,
                None,
                false,
            )),
            "user-token" => Ok(OperationContext::new(
                "normal-user",
                "root",
                /* is_admin */ false,
                None,
                false,
            )),
            _ => Err(tonic::Status::unauthenticated("bad token")),
        }
    }
}

fn sample_record(name: &str, subject_id: &str, admin: bool, revoked: bool) -> AuthKeyRecord {
    AuthKeyRecord {
        key_id: format!("kid-{name}"),
        name: name.to_string(),
        subject_type: SubjectType::User,
        subject_id: subject_id.to_string(),
        is_admin: admin,
        revoked,
        expires_at_ms: None,
        zone_perms: vec![("root".to_string(), "rwx".to_string())],
    }
}

/// Bring up an axum listener on an ephemeral port with pre-seeded
/// store rows.  Returns the base URL + a handle keeping the serve
/// task alive.
async fn spawn_test_server(
    seed: Vec<(String, AuthKeyRecord)>,
) -> (String, Arc<MemStore>, tokio::task::JoinHandle<()>) {
    spawn_test_server_with_secret(seed, None).await
}

/// Variant with an explicit HMAC secret — mint tests need one.  A
/// server built with `None` returns 503 from POST /v2/auth/keys.
async fn spawn_test_server_with_secret(
    seed: Vec<(String, AuthKeyRecord)>,
    secret: Option<&str>,
) -> (String, Arc<MemStore>, tokio::task::JoinHandle<()>) {
    let store = Arc::new(MemStore::default());
    for (hash, record) in seed {
        let bytes = record.encode().expect("encode seed record");
        store.put(&hash, &bytes).expect("seed put");
    }
    let mut state = AppState::for_tests("http://127.0.0.1:1");
    state.auth_key_store = Arc::clone(&store) as Arc<dyn AuthKeyStore>;
    state.auth = Arc::new(FixtureAuth);
    state.api_key_secret = secret.map(Arc::<str>::from);
    let addr: std::net::SocketAddr = "127.0.0.1:0".parse().expect("parse addr");
    let (bound, fut) = bind_and_serve(addr, state).await.expect("bind + serve");
    let handle = tokio::spawn(async move {
        let _ = fut.await;
    });
    (format!("http://{bound}"), store, handle)
}

// ── GET /v2/auth/keys tests ──────────────────────────────────────

/// Happy path: seed 2 records, admin-token GET returns both.
#[tokio::test]
async fn list_returns_seeded_records_for_admin() {
    let (base, _store, _h) = spawn_test_server(vec![
        (
            "hash-alice".to_string(),
            sample_record("alice-key", "alice", false, false),
        ),
        (
            "hash-bob".to_string(),
            sample_record("bob-key", "bob", false, false),
        ),
    ])
    .await;

    let resp: serde_json::Value = reqwest::Client::new()
        .get(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    let keys = resp["keys"].as_array().expect("keys array");
    assert_eq!(keys.len(), 2, "must list both seeded records");
    let names: Vec<_> = keys.iter().map(|k| k["name"].as_str().unwrap()).collect();
    assert!(names.contains(&"alice-key"));
    assert!(names.contains(&"bob-key"));
}

/// A non-admin bearer is rejected with 403 — the router's admin
/// gate MUST override the bearer middleware's "you're authenticated"
/// pass.  Regression pin against a refactor that drops the
/// require_admin call.
#[tokio::test]
async fn list_rejects_non_admin_with_403() {
    let (base, _store, _h) = spawn_test_server(vec![(
        "hash-alice".to_string(),
        sample_record("alice-key", "alice", false, false),
    )])
    .await;

    let resp = reqwest::Client::new()
        .get(format!("{base}/v2/auth/keys"))
        .bearer_auth("user-token")
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status().as_u16(),
        403,
        "authenticated non-admin must be 403 (Forbidden), not 200 or 401",
    );
}

/// No bearer → 401 (bearer middleware catches this before the
/// admin gate).  Pins the layering: middleware first, handler gate
/// second.
#[tokio::test]
async fn list_without_bearer_returns_401() {
    let (base, _store, _h) = spawn_test_server(vec![]).await;
    let resp = reqwest::get(format!("{base}/v2/auth/keys"))
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 401);
}

/// `include_revoked=false` (default) filters revoked rows out.
#[tokio::test]
async fn list_hides_revoked_by_default() {
    let (base, _store, _h) = spawn_test_server(vec![
        (
            "hash-alive".to_string(),
            sample_record("alive-key", "alice", false, false),
        ),
        (
            "hash-dead".to_string(),
            sample_record("dead-key", "bob", false, /* revoked */ true),
        ),
    ])
    .await;

    let resp: serde_json::Value = reqwest::Client::new()
        .get(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    let keys = resp["keys"].as_array().unwrap();
    assert_eq!(keys.len(), 1, "revoked row must be filtered by default");
    assert_eq!(keys[0]["name"], "alive-key");
}

/// `include_revoked=true` surfaces revoked rows for audit.
#[tokio::test]
async fn list_include_revoked_true_shows_revoked() {
    let (base, _store, _h) = spawn_test_server(vec![
        (
            "hash-alive".to_string(),
            sample_record("alive-key", "alice", false, false),
        ),
        (
            "hash-dead".to_string(),
            sample_record("dead-key", "bob", false, true),
        ),
    ])
    .await;

    let resp: serde_json::Value = reqwest::Client::new()
        .get(format!("{base}/v2/auth/keys?include_revoked=true"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    let keys = resp["keys"].as_array().unwrap();
    assert_eq!(keys.len(), 2, "include_revoked=true must show all rows");
}

/// `is_admin=true` filter narrows to admin keys only.
#[tokio::test]
async fn list_filters_by_is_admin() {
    let (base, _store, _h) = spawn_test_server(vec![
        (
            "hash-admin".to_string(),
            sample_record("admin-key", "root", true, false),
        ),
        (
            "hash-user".to_string(),
            sample_record("user-key", "alice", false, false),
        ),
    ])
    .await;

    let resp: serde_json::Value = reqwest::Client::new()
        .get(format!("{base}/v2/auth/keys?is_admin=true"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    let keys = resp["keys"].as_array().unwrap();
    assert_eq!(keys.len(), 1);
    assert_eq!(keys[0]["name"], "admin-key");
}

/// `subject_id=alice` narrows to that principal's keys.
#[tokio::test]
async fn list_filters_by_subject_id() {
    let (base, _store, _h) = spawn_test_server(vec![
        (
            "hash-a1".to_string(),
            sample_record("a1", "alice", false, false),
        ),
        (
            "hash-a2".to_string(),
            sample_record("a2", "alice", false, false),
        ),
        (
            "hash-b".to_string(),
            sample_record("b", "bob", false, false),
        ),
    ])
    .await;

    let resp: serde_json::Value = reqwest::Client::new()
        .get(format!("{base}/v2/auth/keys?subject_id=alice"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    let keys = resp["keys"].as_array().unwrap();
    assert_eq!(keys.len(), 2, "must include both of alice's keys");
    for k in keys {
        assert_eq!(k["subject_id"], "alice");
    }
}

/// A row this build cannot decode (schema drift / corrupt bytes)
/// is soft-skipped — the listing carries the other rows through.
/// Regression pin against a bug where a single bad row wedges the
/// whole listing.
#[tokio::test]
async fn list_soft_skips_undecodable_row() {
    let store = Arc::new(MemStore::default());
    // Well-formed row.
    let good = sample_record("good", "alice", false, false)
        .encode()
        .unwrap();
    store.put("hash-good", &good).unwrap();
    // Corrupt row (not JSON).
    store.put("hash-bad", b"\xff\xfe not-json").unwrap();

    let mut state = AppState::for_tests("http://127.0.0.1:1");
    state.auth_key_store = Arc::clone(&store) as Arc<dyn AuthKeyStore>;
    state.auth = Arc::new(FixtureAuth);
    let addr: std::net::SocketAddr = "127.0.0.1:0".parse().unwrap();
    let (bound, fut) = bind_and_serve(addr, state).await.unwrap();
    let _h = tokio::spawn(async move {
        let _ = fut.await;
    });

    let resp: serde_json::Value = reqwest::Client::new()
        .get(format!("http://{bound}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    let keys = resp["keys"].as_array().unwrap();
    assert_eq!(
        keys.len(),
        1,
        "one bad row must NOT wedge the listing — the good row stays visible",
    );
    assert_eq!(keys[0]["name"], "good");
}

// ── DELETE /v2/auth/keys/:key_hash tests ─────────────────────────

/// Happy path: revoke an existing key returns 200 with existed=true;
/// the store row is really gone.
#[tokio::test]
async fn revoke_existing_removes_row_and_reports_existed_true() {
    let (base, store, _h) = spawn_test_server(vec![(
        "hash-a".to_string(),
        sample_record("a", "alice", false, false),
    )])
    .await;

    let resp: serde_json::Value = reqwest::Client::new()
        .delete(format!("{base}/v2/auth/keys/hash-a"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    assert_eq!(resp["existed"], true);
    assert_eq!(resp["key_hash"], "hash-a");
    assert!(
        store.get("hash-a").unwrap().is_none(),
        "store row must be really gone, not just reported as removed",
    );
}

/// Revoke on missing hash → 200 with existed=false (idempotent).
#[tokio::test]
async fn revoke_missing_reports_existed_false() {
    let (base, _store, _h) = spawn_test_server(vec![]).await;
    let resp: serde_json::Value = reqwest::Client::new()
        .delete(format!("{base}/v2/auth/keys/hash-nope"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");
    assert_eq!(resp["existed"], false);
    assert_eq!(resp["key_hash"], "hash-nope");
}

/// Non-admin bearer → 403 (revoke is admin-only).
#[tokio::test]
async fn revoke_rejects_non_admin_with_403() {
    let (base, store, _h) = spawn_test_server(vec![(
        "hash-a".to_string(),
        sample_record("a", "alice", false, false),
    )])
    .await;
    let resp = reqwest::Client::new()
        .delete(format!("{base}/v2/auth/keys/hash-a"))
        .bearer_auth("user-token")
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 403);
    assert!(
        store.get("hash-a").unwrap().is_some(),
        "denied revoke must NOT touch the store — the row must survive",
    );
}

/// No bearer → 401 (bearer middleware). Pins the layer order:
/// authN first (middleware), authZ second (handler admin gate).
#[tokio::test]
async fn revoke_without_bearer_returns_401() {
    let (base, _store, _h) = spawn_test_server(vec![]).await;
    let resp = reqwest::Client::new()
        .delete(format!("{base}/v2/auth/keys/hash-x"))
        .body(json!({}).to_string())
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 401);
}

// ── POST /v2/auth/keys (mint) tests ──────────────────────────────

/// Happy path: mint a `user` key with a zone grant.  Response
/// includes the plaintext key + hash + key_id + record echo.  The
/// store row is really present after mint.
#[tokio::test]
async fn mint_user_key_with_zone_grant_returns_key_and_persists_row() {
    let (base, store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;

    let body = json!({
        "name": "alice-laptop",
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["root:rwx"],
        "admin": false,
    });
    let resp: serde_json::Value = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send")
        .json()
        .await
        .expect("json");

    let key = resp["key"].as_str().expect("key present");
    let key_hash = resp["key_hash"].as_str().expect("key_hash present");
    assert!(key.starts_with("sk-"), "plaintext key must be sk-prefixed");
    assert!(
        !key_hash.is_empty(),
        "key_hash must be present for later revoke",
    );
    assert_eq!(resp["record"]["name"], "alice-laptop");
    assert_eq!(resp["record"]["subject_type"], "user");
    assert_eq!(resp["record"]["subject_id"], "alice");
    assert_eq!(resp["record"]["is_admin"], false);
    assert!(
        store.get(key_hash).unwrap().is_some(),
        "mint must persist the row at the returned hash",
    );
}

/// Admin key with no zone grants — the zoneless-admin-only rule.
#[tokio::test]
async fn mint_admin_key_without_zones_is_allowed() {
    let (base, _store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "name": "root-admin",
        "subject_type": "user",
        "subject_id": "root",
        "admin": true,
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 200);
}

/// Non-admin key with NO zone grants → 400 (a key with no grants
/// reaches nothing; the mint layer refuses it).  Pin against a
/// regression that silently mints an unreachable key.
#[tokio::test]
async fn mint_non_admin_without_zones_returns_400() {
    let (base, _store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "name": "unreachable",
        "subject_type": "user",
        "subject_id": "alice",
        "admin": false,
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status().as_u16(),
        400,
        "no-grants-no-admin must be 400 (client input error)",
    );
}

/// A malformed zone grant → 400.  Message names the segment.
#[tokio::test]
async fn mint_malformed_zone_grant_returns_400() {
    let (base, _store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["missing-colon-here"],
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 400);
}

/// `subject_type=agent` is rejected — agents use the cert-anchored
/// plane, not sk-.
#[tokio::test]
async fn mint_agent_subject_type_returns_400() {
    let (base, _store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "subject_type": "agent",
        "subject_id": "some-agent",
        "zones": ["root:rwx"],
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 400);
}

/// Repeat mint for the same `(subject_type, subject_id)` without
/// `allow_existing` → 400 (uniqueness rejection reclassified from
/// the mint layer's Backend error).
#[tokio::test]
async fn mint_repeat_same_subject_returns_400_without_allow_existing() {
    let (base, _store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["root:rwx"],
    });
    // First mint succeeds.
    let r1 = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send1");
    assert_eq!(r1.status().as_u16(), 200);
    // Second without allow_existing → 400.
    let r2 = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send2");
    assert_eq!(
        r2.status().as_u16(),
        400,
        "duplicate subject without allow_existing must be 400 (input error), not 502",
    );
}

/// `allow_existing=true` lets a rotation land — two keys for the
/// same subject coexist (the audit path).
#[tokio::test]
async fn mint_allow_existing_true_allows_rotation() {
    let (base, store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["root:rwx"],
        "allow_existing": true,
    });
    reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("mint1");
    reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("mint2");
    assert_eq!(
        store.list().unwrap().len(),
        2,
        "allow_existing=true must let two rows coexist for the same subject",
    );
}

/// No HMAC secret configured → 503 (auth-off daemon; sk- plane
/// unavailable).
#[tokio::test]
async fn mint_without_secret_returns_503() {
    let (base, _store, _h) = spawn_test_server(vec![]).await; // secret=None
    let body = json!({
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["root:rwx"],
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(
        resp.status().as_u16(),
        503,
        "no secret → 503 (service unavailable), matches gRPC MintKey posture under NoAuth",
    );
}

/// Non-admin bearer → 403 even with a valid secret + body.
#[tokio::test]
async fn mint_rejects_non_admin_with_403() {
    let (base, _store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let body = json!({
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["root:rwx"],
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("user-token")
        .json(&body)
        .send()
        .await
        .expect("send");
    assert_eq!(resp.status().as_u16(), 403);
}

/// End-to-end mint → revoke: the minted key's hash from the
/// response body is a valid revoke target.
#[tokio::test]
async fn minted_key_can_be_revoked_by_returned_hash() {
    let (base, store, _h) = spawn_test_server_with_secret(vec![], Some("test-hmac-secret")).await;
    let mint_body = json!({
        "subject_type": "user",
        "subject_id": "alice",
        "zones": ["root:rwx"],
    });
    let mint_resp: serde_json::Value = reqwest::Client::new()
        .post(format!("{base}/v2/auth/keys"))
        .bearer_auth("admin-token")
        .json(&mint_body)
        .send()
        .await
        .expect("mint")
        .json()
        .await
        .expect("mint json");
    let hash = mint_resp["key_hash"].as_str().unwrap();
    let revoke: serde_json::Value = reqwest::Client::new()
        .delete(format!("{base}/v2/auth/keys/{hash}"))
        .bearer_auth("admin-token")
        .send()
        .await
        .expect("revoke")
        .json()
        .await
        .expect("revoke json");
    assert_eq!(revoke["existed"], true);
    assert!(
        store.get(hash).unwrap().is_none(),
        "revoke of just-minted hash must actually remove the row",
    );
}
