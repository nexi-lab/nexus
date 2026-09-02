//! Integration coverage for the `/v2/rebac/tuples` router.
//!
//! Real-chain shape: bind an ephemeral axum listener over an
//! `AppState` whose `rebac_store` is an `InMemoryReBACTupleStore`
//! (the raft-backed store needs a live 1-voter zone — covered in
//! the nexus-rebac unit tests, not needed here).  Grants written
//! via HTTP land in the same store a `list` reads back, so the
//! wire and the SSOT stay in sync end-to-end.
//!
//! Gate: `#[cfg(feature = "rebac")]` — the test file is a no-op
//! under the slim `http-api`-only build, matching the crate's
//! feature-gated router.

#![cfg(feature = "rebac")]

use std::sync::Arc;

use nexus_http_api::{bind_and_serve, AppState};
use nexus_rebac::InMemoryReBACTupleStore;
use serde_json::json;

/// Bring up an axum listener on an ephemeral port with a fresh
/// in-memory rebac store.  Returns the base URL callers `POST` /
/// `GET` / `DELETE` against + a handle keeping the serve task
/// alive (dropped at end of scope — tokio cancels the listener).
async fn spawn_test_server() -> (String, tokio::task::JoinHandle<()>) {
    let mut state = AppState::for_tests("http://127.0.0.1:1"); // never dialed
    state.rebac_store = Arc::new(InMemoryReBACTupleStore::new());
    let addr: std::net::SocketAddr = "127.0.0.1:0".parse().expect("parse addr");
    let (bound, fut) = bind_and_serve(addr, state).await.expect("bind + serve");
    let handle = tokio::spawn(async move {
        let _ = fut.await;
    });
    (format!("http://{bound}"), handle)
}

/// POST `/v2/rebac/tuples` — direct-relation grant.  Immediately
/// GET `?zone=...` and assert the tuple is listed.  Pins the "wire
/// grant lands in the store" invariant end-to-end (encode → put →
/// list → decode → JSON body match).
#[tokio::test]
async fn grant_then_list_reflects_the_new_tuple() {
    let (base, _h) = spawn_test_server().await;
    let client = reqwest::Client::new();

    let body = json!({
        "zone": "root",
        "object_type": "file",
        "object_id": "/root/doc",
        "relation": "reader",
        "subject_type": "user",
        "subject_id": "alice",
    });
    let resp = client
        .post(format!("{base}/v2/rebac/tuples"))
        .json(&body)
        .send()
        .await
        .expect("grant send");
    assert_eq!(resp.status().as_u16(), 200, "grant must return 200");

    let listed = client
        .get(format!("{base}/v2/rebac/tuples?zone=root"))
        .send()
        .await
        .expect("list send")
        .json::<serde_json::Value>()
        .await
        .expect("list body");
    assert_eq!(
        listed["tuples"].as_array().expect("tuples array").len(),
        1,
        "the fresh zone must have exactly the grant we just wrote",
    );
    let t = &listed["tuples"][0];
    assert_eq!(t["zone"], "root");
    assert_eq!(t["object_type"], "file");
    assert_eq!(t["object_id"], "/root/doc");
    assert_eq!(t["relation"], "reader");
    assert_eq!(t["subject_type"], "user");
    assert_eq!(t["subject_id"], "alice");
    assert!(
        t.get("subject_relation").is_none() || t["subject_relation"].is_null(),
        "direct tuple must not emit a subject_relation field",
    );
}

/// Userset-as-subject grant (Zanzibar `subject_relation`) — the
/// 7-segment path.  Pins that the extra field roundtrips through
/// the wire encoder / decoder faithfully.
#[tokio::test]
async fn grant_userset_tuple_roundtrips_subject_relation() {
    let (base, _h) = spawn_test_server().await;
    let client = reqwest::Client::new();

    let body = json!({
        "zone": "root",
        "object_type": "file",
        "object_id": "/root/team-doc",
        "relation": "reader",
        "subject_type": "group",
        "subject_id": "eng",
        "subject_relation": "member",
    });
    let resp = client
        .post(format!("{base}/v2/rebac/tuples"))
        .json(&body)
        .send()
        .await
        .expect("grant send");
    assert_eq!(resp.status().as_u16(), 200);

    let listed = client
        .get(format!("{base}/v2/rebac/tuples?zone=root"))
        .send()
        .await
        .expect("list send")
        .json::<serde_json::Value>()
        .await
        .expect("list body");
    assert_eq!(listed["tuples"][0]["subject_relation"], "member");
}

/// DELETE `/v2/rebac/tuples` — advisory `existed` = true on a
/// present tuple, false on a missing one (idempotent).  Also
/// asserts the grant is gone from the following `GET` — the store
/// really removed it, not just reported success.
#[tokio::test]
async fn revoke_removes_the_tuple_and_reports_existence() {
    let (base, _h) = spawn_test_server().await;
    let client = reqwest::Client::new();

    let body = json!({
        "zone": "root",
        "object_type": "file",
        "object_id": "/root/doc",
        "relation": "reader",
        "subject_type": "user",
        "subject_id": "alice",
    });
    // Grant first.
    client
        .post(format!("{base}/v2/rebac/tuples"))
        .json(&body)
        .send()
        .await
        .expect("grant");

    // Revoke — must report existed=true.
    let revoke_resp = client
        .delete(format!("{base}/v2/rebac/tuples"))
        .json(&body)
        .send()
        .await
        .expect("revoke send")
        .json::<serde_json::Value>()
        .await
        .expect("revoke body");
    assert_eq!(revoke_resp["existed"], true, "must report existed=true");

    // Re-revoke — must report existed=false (idempotent).
    let re_revoke = client
        .delete(format!("{base}/v2/rebac/tuples"))
        .json(&body)
        .send()
        .await
        .expect("re-revoke send")
        .json::<serde_json::Value>()
        .await
        .expect("re-revoke body");
    assert_eq!(
        re_revoke["existed"], false,
        "second revoke on the same tuple must report existed=false — \
         the response gives a caller an audit signal without paying \
         a not-found error",
    );

    // Listing must now be empty.
    let listed = client
        .get(format!("{base}/v2/rebac/tuples?zone=root"))
        .send()
        .await
        .expect("list send")
        .json::<serde_json::Value>()
        .await
        .expect("list body");
    assert!(
        listed["tuples"].as_array().unwrap().is_empty(),
        "the store must actually remove the tuple, not just report success",
    );
}

/// GET `?zone=` (empty) — 400, not 500 or empty result.  Pins the
/// "zone is required" contract at the boundary.
#[tokio::test]
async fn list_without_zone_returns_400() {
    let (base, _h) = spawn_test_server().await;
    let resp = reqwest::get(format!("{base}/v2/rebac/tuples?zone="))
        .await
        .expect("list send");
    assert_eq!(
        resp.status().as_u16(),
        400,
        "empty zone must be rejected with 400, not silently listed",
    );
}

/// Zone isolation on the wire: a grant in zone A does NOT appear
/// in a listing of zone B, and vice-versa.  Pins the store's
/// per-zone scoping through the HTTP surface.
#[tokio::test]
async fn zone_isolation_grant_in_a_invisible_in_b() {
    let (base, _h) = spawn_test_server().await;
    let client = reqwest::Client::new();

    let mk = |zone: &str| {
        json!({
            "zone": zone,
            "object_type": "file",
            "object_id": "/doc",
            "relation": "reader",
            "subject_type": "user",
            "subject_id": "alice",
        })
    };

    client
        .post(format!("{base}/v2/rebac/tuples"))
        .json(&mk("zone_a"))
        .send()
        .await
        .expect("grant A");

    let listed_b: serde_json::Value = client
        .get(format!("{base}/v2/rebac/tuples?zone=zone_b"))
        .send()
        .await
        .expect("list B")
        .json()
        .await
        .expect("list B body");
    assert!(
        listed_b["tuples"].as_array().unwrap().is_empty(),
        "zone_b must not see zone_a's grant — the store's per-zone \
         scoping must reach the HTTP surface",
    );
}

/// A tuple segment containing the reserved `|` delimiter is a
/// client input error — 400, not 502.  Regression pin against
/// classifying every store error as a backend failure.
#[tokio::test]
async fn grant_with_pipe_in_segment_returns_400() {
    let (base, _h) = spawn_test_server().await;
    let body = json!({
        "zone": "root",
        "object_type": "file",
        "object_id": "/root/bad|id",
        "relation": "reader",
        "subject_type": "user",
        "subject_id": "alice",
    });
    let resp = reqwest::Client::new()
        .post(format!("{base}/v2/rebac/tuples"))
        .json(&body)
        .send()
        .await
        .expect("grant send");
    assert_eq!(
        resp.status().as_u16(),
        400,
        "a pipe in a segment is client input error (400), not backend (502)",
    );
}

/// A repeat grant on the same tuple is idempotent — 200 both
/// times, and the listing shows exactly one entry (no dupes).
/// Pins the idempotent-put contract at the HTTP boundary.
#[tokio::test]
async fn repeat_grant_is_idempotent_no_duplicate_listing() {
    let (base, _h) = spawn_test_server().await;
    let client = reqwest::Client::new();
    let body = json!({
        "zone": "root",
        "object_type": "file",
        "object_id": "/doc",
        "relation": "reader",
        "subject_type": "user",
        "subject_id": "alice",
    });
    for _ in 0..3 {
        let r = client
            .post(format!("{base}/v2/rebac/tuples"))
            .json(&body)
            .send()
            .await
            .expect("grant");
        assert_eq!(r.status().as_u16(), 200);
    }
    let listed: serde_json::Value = client
        .get(format!("{base}/v2/rebac/tuples?zone=root"))
        .send()
        .await
        .expect("list")
        .json()
        .await
        .expect("list body");
    assert_eq!(
        listed["tuples"].as_array().unwrap().len(),
        1,
        "3 identical grants must produce exactly 1 listed entry — \
         a regression would surface as a duplicate row a graph rebuild \
         silently deduplicates (masking the write-side bug)",
    );
}
