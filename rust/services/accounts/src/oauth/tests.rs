//! Real HTTP/gRPC and SQL with only the external OIDC provider substituted.

use super::*;
use crate::proto::*;
use serde_json::{json, Value};
use tonic::Request;

mod account_closure;
mod provider;
mod registration;
mod support;
use provider::ProviderFixture;
use support::*;

#[tokio::test]
async fn google_callback_survives_replica_restart_without_persisting_flow_or_provider_tokens() {
    for postgres in databases() {
        let database = DatabaseFixture::new(postgres).await;
        let provider = ProviderFixture::new().await;
        let first = Running::new(&database, &provider).await;
        let (start, cookie) = first.begin().await;
        let (code, state) = provider
            .code(start["auth_url"].as_str().unwrap(), json!({"is_admin":true,"is_global_admin":true,"zone_id":"forged-zone","user_id":"forged-owner"}), 1, 1)
            .await;
        first.close().await;
        assert_eq!(database.count("SELECT count(*) FROM users").await, 0);
        assert_eq!(
            database
                .count("SELECT count(*) FROM user_oauth_accounts")
                .await,
            0
        );
        let second = Running::new(&database, &provider).await;
        let (status, login) = second.callback(&code, &state, &cookie).await;
        assert_eq!(status, 200, "{login}");
        let user = &login["user"];
        assert_eq!(user["primary_auth_method"], "oauth");
        assert_eq!(user["email_verified"], true);
        assert_eq!(user["is_global_admin"], false);
        assert!(user["zone_id"].is_null());
        assert!(user["username"].is_null());
        assert_eq!(user["display_name"], "Google User");
        let mut request = Request::new(GetProfileRequest {});
        request.metadata_mut().insert(
            "authorization",
            format!("Bearer {}", login["token"].as_str().unwrap())
                .parse()
                .unwrap(),
        );
        let profile = second
            .client()
            .await
            .get_profile(request)
            .await
            .unwrap()
            .into_inner();
        assert_eq!(profile.user_id, user["user_id"].as_str().unwrap());
        assert!(!login.as_object().unwrap().contains_key("api_key"));
        assert_eq!(database.count("SELECT count(*) FROM users WHERE password_hash IS NULL AND api_key IS NULL AND zone_id IS NULL AND external_user_id IS NULL AND user_metadata IS NULL").await, 1);
        assert_eq!(database.count("SELECT count(*) FROM user_oauth_accounts WHERE encrypted_id_token IS NULL AND token_expires_at IS NULL AND provider_profile IS NULL").await, 1);
        assert_eq!(second.callback(&code, &state, &cookie).await.0, 401);
        let existing_id = profile.user_id;
        second.close().await;
        let third = Running::new(&database, &provider).await;
        let mut client = third.client().await;
        let start = client
            .start_google_login(StartGoogleLoginRequest {})
            .await
            .unwrap()
            .into_inner();
        let (code, state) = provider
            .code(
                &start.auth_url,
                json!({"email":"changed-provider-email@example.test"}),
                1,
                1,
            )
            .await;
        let login = client
            .finish_google_login(FinishGoogleLoginRequest {
                code,
                state,
                browser_binding: start.browser_binding,
            })
            .await
            .unwrap()
            .into_inner();
        let user = login.user.unwrap();
        assert_eq!(user.user_id, existing_id);
        assert_eq!(user.email.as_deref(), Some("google@example.test"));
        assert_eq!(
            database
                .text("SELECT provider_email FROM user_oauth_accounts")
                .await
                .as_deref(),
            Some("changed-provider-email@example.test")
        );
        assert_eq!(database.count("SELECT count(*) FROM users").await, 1);
        third.close().await;
        database.clean().await;
    }
}

#[tokio::test]
async fn provider_subject_preserves_legacy_owner_and_never_links_by_email_or_recreates_disabled_accounts(
) {
    for postgres in databases() {
        let database = DatabaseFixture::new(postgres).await;
        database.execute("INSERT INTO users (user_id,email,username,display_name,password_hash,primary_auth_method,is_global_admin,is_active,email_verified,zone_id,created_at,updated_at,user_metadata) VALUES ('legacy-id','legacy@example.test','legacy-name','Edited Profile','legacy-password-hash','password',1,1,1,'existing-zone','2000-01-01 00:00:00','2000-01-01 00:00:00','{\"preserved\":true}'); INSERT INTO user_oauth_accounts (oauth_account_id,user_id,provider,provider_user_id,provider_email,created_at) VALUES ('old-binding','legacy-id','google','legacy-subject','legacy@example.test','2000-01-01 00:00:00');").await;
        let provider = ProviderFixture::new().await;
        let running = Running::new(&database, &provider).await;
        let (start, cookie) = running.begin().await;
        let (code, state) = provider.code(start["auth_url"].as_str().unwrap(), json!({"sub":"legacy-subject","email":"changed@example.test","name":"Provider Name"}), 1, 1).await;
        let (status, login) = running.callback(&code, &state, &cookie).await;
        assert_eq!(status, 200, "{login}");
        assert_eq!(login["user"]["user_id"], "legacy-id");
        assert_eq!(login["user"]["display_name"], "Edited Profile");
        assert_eq!(login["user"]["email"], "legacy@example.test");
        assert_eq!(login["user"]["zone_id"], "existing-zone");
        assert_eq!(
            database
                .text("SELECT password_hash FROM users")
                .await
                .as_deref(),
            Some("legacy-password-hash")
        );
        assert_eq!(
            database
                .text("SELECT user_metadata FROM users")
                .await
                .as_deref(),
            Some("{\"preserved\":true}")
        );
        for email in ["legacy@example.test", "new-provider@example.test"] {
            if email.starts_with("new-") {
                database.execute("INSERT INTO users (user_id,email,primary_auth_method,is_global_admin,is_active,email_verified,created_at,updated_at) VALUES ('unverified','new-provider@example.test','password',0,1,0,'2000-01-01 00:00:00','2000-01-01 00:00:00')").await;
            }
            let (start, cookie) = running.begin().await;
            let (code, state) = provider
                .code(
                    start["auth_url"].as_str().unwrap(),
                    json!({"sub":"different-google-subject","email":email}),
                    1,
                    1,
                )
                .await;
            assert_eq!(running.callback(&code, &state, &cookie).await.0, 409);
        }
        for transition in [
            "is_active = 0",
            "is_active = 1, deleted_at = '2001-01-01 00:00:00'",
            "deleted_at = NULL, email_verified = 0",
        ] {
            database
                .execute(&format!(
                    "UPDATE users SET {transition} WHERE user_id = 'legacy-id'"
                ))
                .await;
            let (start, cookie) = running.begin().await;
            let (code, state) = provider
                .code(
                    start["auth_url"].as_str().unwrap(),
                    json!({"sub":"legacy-subject","email":"changed@example.test"}),
                    1,
                    1,
                )
                .await;
            assert_eq!(running.callback(&code, &state, &cookie).await.0, 401);
        }
        assert_eq!(database.count("SELECT count(*) FROM users").await, 2);
        assert_eq!(
            database
                .count("SELECT count(*) FROM user_oauth_accounts")
                .await,
            1
        );
        running.close().await;
        database.clean().await;
    }
}

#[tokio::test]
async fn rejects_wrong_oidc_claims_and_browser_binding_before_any_identity_write() {
    let database = DatabaseFixture::new(false).await;
    let provider = ProviderFixture::new().await;
    let running = Running::new(&database, &provider).await;
    let now = Utc::now().timestamp();
    let invalid_claims = [
        json!({"iss":"https://untrusted.example.test"}),
        json!({"aud":"other-client"}),
        json!({"aud":["fixture-client","untrusted-client"]}),
        json!({"azp":"other-client"}),
        json!({"nonce":"other-browser"}),
        json!({"nonce":null}),
        json!({"exp":now-1}),
        json!({"nbf":now+120}),
        json!({"exp":null}),
        json!({"iat":now+120}),
        json!({"exp":now+100, "iat":now+101}),
        json!({"sub":""}),
        json!({"sub":null}),
        json!({"sub":"x".repeat(256)}),
        json!({"email_verified":false}),
        json!({"email_verified":null}),
        json!({"email":"not-an-email"}),
        json!({"email":null}),
        json!({"at_hash":"wrong-access-token-hash"}),
    ];
    for claims in invalid_claims {
        let (start, cookie) = running.begin().await;
        let (code, state) = provider
            .code(start["auth_url"].as_str().unwrap(), claims.clone(), 1, 1)
            .await;
        let (status, error) = running.callback(&code, &state, &cookie).await;
        assert_eq!(status, 401, "{claims}: {error}");
    }
    // A known kid with a signature from another key must fail; a missing kid
    // must not cause an unbounded discovery refresh on each request.
    for (signing, header) in [(0, 1), (2, 1), (2, 2), (2, 2)] {
        let (start, cookie) = running.begin().await;
        let (code, state) = provider
            .code(
                start["auth_url"].as_str().unwrap(),
                json!({}),
                signing,
                header,
            )
            .await;
        assert_eq!(running.callback(&code, &state, &cookie).await.0, 401);
    }
    assert_eq!(provider.discoveries(), 2);
    let (start, cookie) = running.begin().await;
    let (code, state) = provider
        .code(start["auth_url"].as_str().unwrap(), json!({}), 1, 1)
        .await;
    let hits = provider.token_requests();
    for cookie in [
        "".to_string(),
        "nexus_google=wrong".into(),
        format!("{cookie}; {cookie}"),
        format!("nexus_google={}", "x".repeat(43)),
    ] {
        assert_eq!(running.callback(&code, &state, &cookie).await.0, 401);
    }
    let mut flow: Flow = decode::<Flow>(
        &state,
        &DecodingKey::from_secret(SECRET),
        &running.service.google().unwrap().validation,
    )
    .unwrap()
    .claims;
    let forged = encode(
        &Header::new(Algorithm::HS256),
        &flow,
        &EncodingKey::from_secret(b"other-signing-key-not-the-account-secret"),
    )
    .unwrap();
    assert_eq!(running.callback(&code, &forged, &cookie).await.0, 401);
    for change in [0, 1, 2, 3] {
        match change {
            0 => flow.purpose = "account".into(),
            1 => {
                flow.purpose = "google_login".into();
                flow.client_id = "other-client".into();
            }
            2 => {
                flow.client_id = "fixture-client".into();
                flow.redirect_uri = "https://attacker.example.test/callback".into();
            }
            _ => {
                flow.redirect_uri = REDIRECT.into();
                flow.iat = now as u64 - 601;
                flow.exp = now as u64 - 1;
            }
        }
        let state = encode(
            &Header::new(Algorithm::HS256),
            &flow,
            &EncodingKey::from_secret(SECRET),
        )
        .unwrap();
        assert_eq!(running.callback(&code, &state, &cookie).await.0, 401);
    }
    assert_eq!(provider.token_requests(), hits);
    assert_eq!(database.count("SELECT count(*) FROM users").await, 0);
    assert_eq!(
        database
            .count("SELECT count(*) FROM user_oauth_accounts")
            .await,
        0
    );
    assert_eq!(running.callback(&code, &state, &cookie).await.0, 200);
    running.close().await;
    database.clean().await;
}

#[tokio::test]
async fn google_key_rotation_refreshes_once_and_does_not_repeat_the_single_use_exchange() {
    let database = DatabaseFixture::new(false).await;
    let provider = ProviderFixture::new().await;
    let running = Running::new(&database, &provider).await;
    provider.rotate().await;
    let (start, cookie) = running.begin().await;
    let (code, state) = provider
        .code(start["auth_url"].as_str().unwrap(), json!({}), 2, 2)
        .await;
    assert_eq!(running.callback(&code, &state, &cookie).await.0, 200);
    assert_eq!(provider.token_requests(), 1);
    assert_eq!(provider.discoveries(), 2);
    running.close().await;
    database.clean().await;
}

#[tokio::test]
async fn independent_replicas_share_one_transaction_for_google_and_password_registration() {
    for postgres in databases() {
        for scenario in 0..4 {
            let database = DatabaseFixture::new(postgres).await;
            let provider = ProviderFixture::new().await;
            let first = Running::new(&database, &provider).await;
            let second = Running::new(&database, &provider).await;
            let (start, cookie) = first.begin().await;
            let (code, state) = provider
                .code(
                    start["auth_url"].as_str().unwrap(),
                    json!({"sub":"first-subject","email":"race@example.test"}),
                    1,
                    1,
                )
                .await;
            let mut client = second.client().await;
            let start = client
                .start_google_login(StartGoogleLoginRequest {})
                .await
                .unwrap()
                .into_inner();
            let claims = match scenario {
                0 => json!({"sub":"first-subject","email":"second-email@example.test"}),
                1 => json!({"sub":"second-subject","email":"race@example.test"}),
                _ => json!({"sub":"second-subject","email":"independent@example.test"}),
            };
            let (second_code, second_state) = provider.code(&start.auth_url, claims, 1, 1).await;
            let gate = database.block_writes().await;
            let release = async {
                database
                    .wait_for_two_writers(&provider, if scenario == 2 { 1 } else { 2 })
                    .await;
                gate.release().await;
            };
            let requests = async {
                tokio::join!(first.callback(&code, &state, &cookie), async {
                    if scenario == 2 {
                        match client
                            .register(RegisterRequest {
                                email: "race@example.test".into(),
                                password: "registration-race-password".into(),
                                username: None,
                                display_name: None,
                            })
                            .await
                        {
                            Ok(response) => {
                                (201, serde_json::to_value(response.into_inner()).unwrap())
                            }
                            Err(error) => {
                                assert_eq!(error.code(), tonic::Code::AlreadyExists);
                                (409, json!({}))
                            }
                        }
                    } else {
                        match client
                            .finish_google_login(FinishGoogleLoginRequest {
                                code: second_code,
                                state: second_state,
                                browser_binding: start.browser_binding,
                            })
                            .await
                        {
                            Ok(response) => {
                                (200, serde_json::to_value(response.into_inner()).unwrap())
                            }
                            Err(error) => {
                                assert_eq!(error.code(), tonic::Code::AlreadyExists);
                                (409, json!({}))
                            }
                        }
                    }
                })
            };
            let (((first_result, first_body), (second_result, second_body)), ()) =
                tokio::join!(requests, release);
            match scenario {
                0 => {
                    assert_eq!(
                        (first_result, second_result),
                        (200, 200),
                        "{first_body} {second_body}"
                    );
                    assert_eq!(
                        first_body["user"]["user_id"],
                        second_body["user"]["user_id"]
                    );
                    assert_eq!(database.count("SELECT count(*) FROM users").await, 1);
                    assert_eq!(
                        database
                            .count("SELECT count(*) FROM user_oauth_accounts")
                            .await,
                        1
                    );
                }
                1 | 2 => {
                    assert!(
                        matches!(
                            (first_result, second_result),
                            (200, 409) | (409, 200) | (409, 201)
                        ),
                        "{scenario}: {first_result} {second_result}: {first_body} {second_body}"
                    );
                    assert_eq!(database.count("SELECT count(*) FROM users").await, 1);
                    assert_eq!(database.count("SELECT count(*) FROM user_oauth_accounts a LEFT JOIN users u ON a.user_id = u.user_id WHERE u.user_id IS NULL").await, 0);
                }
                _ => {
                    assert_eq!((first_result, second_result), (200, 200));
                    assert_ne!(
                        first_body["user"]["user_id"],
                        second_body["user"]["user_id"]
                    );
                    assert_eq!(database.count("SELECT count(*) FROM users").await, 2);
                    assert_eq!(
                        database
                            .count("SELECT count(*) FROM user_oauth_accounts")
                            .await,
                        2
                    );
                }
            }
            first.close().await;
            second.close().await;
            database.clean().await;
        }
    }
}

#[tokio::test]
async fn google_transport_rejects_json_binding_and_preserves_fixed_redirect_and_token_purpose() {
    let database = DatabaseFixture::new(false).await;
    let provider = ProviderFixture::new().await;
    let google = google_redirect(&provider, "https://app.example.test/oauth/callback").await;
    let running = Running::with_google(&database, google).await;
    let http = reqwest::Client::new();
    let response = http
        .get(format!(
            "{}/auth/oauth/google/authorize?redirect_uri=https://attacker.example.test",
            running.http_url
        ))
        .send()
        .await
        .unwrap();
    let cookie = response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .to_owned();
    assert!(cookie.starts_with("__Host-nexus_google="));
    assert!(
        cookie.contains("; Secure") && cookie.contains("; Path=/") && cookie.contains("; HttpOnly")
    );
    assert!(!cookie.contains("Domain="));
    let start: Value = response.json().await.unwrap();
    let auth = url::Url::parse(start["auth_url"].as_str().unwrap()).unwrap();
    assert_eq!(
        auth.query_pairs()
            .find(|(key, _)| key == "redirect_uri")
            .unwrap()
            .1,
        "https://app.example.test/oauth/callback"
    );
    let state = start["state"].as_str().unwrap();
    let binding = cookie.split(';').next().unwrap().split_once('=').unwrap().1;
    assert!(!state.contains(binding));
    let (code, _) = provider
        .code(start["auth_url"].as_str().unwrap(), json!({}), 1, 1)
        .await;
    let response = http
        .post(format!("{}/auth/oauth/google/callback", running.http_url))
        .header("cookie", cookie.split(';').next().unwrap())
        .json(&json!({"code":code,"state":state,"browser_binding":binding}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 422);
    assert!(response.headers()["set-cookie"]
        .to_str()
        .unwrap()
        .contains("Max-Age=0"));
    let response = http
        .get(format!("{}/auth/me", running.http_url))
        .bearer_auth(state)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 401);
    let response = http
        .post(format!("{}/auth/verify-email", running.http_url))
        .json(&json!({"token":state}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 400);
    assert_eq!(provider.token_requests(), 0);
    assert_eq!(database.count("SELECT count(*) FROM users").await, 0);
    running.close().await;
    database.clean().await;
}

#[tokio::test]
async fn google_configuration_rejects_partial_identity_and_unsafe_redirect_before_network_access() {
    for redirect in [
        "http://remote.example.test/callback",
        "https://user:password@example.test/callback",
        "https://example.test/callback?next=other",
        "https://example.test/callback#fragment",
        "file:///callback",
    ] {
        let result = GoogleLogin::new(
            "fixture-client".into(),
            "fixture-secret".into(),
            redirect.into(),
            SECRET,
        )
        .await;
        assert_eq!(
            result.err().expect("Unsafe redirect accepted").code(),
            tonic::Code::InvalidArgument
        );
    }
    for (id, secret, signing_key) in [
        ("", "fixture-secret", SECRET),
        ("fixture-client", "", SECRET),
        ("fixture-client", "fixture-secret", b"short".as_slice()),
    ] {
        let result = GoogleLogin::new(id.into(), secret.into(), REDIRECT.into(), signing_key).await;
        assert_eq!(
            result.err().expect("Invalid credentials accepted").code(),
            tonic::Code::InvalidArgument
        );
    }
}

#[tokio::test]
async fn expired_google_cache_and_large_responses_fail_closed_with_bounded_provider_work() {
    let database = DatabaseFixture::new(false).await;
    let provider = ProviderFixture::new().await;
    let running = Running::new(&database, &provider).await;
    let google = running.service.google().unwrap();
    {
        let mut cache = google.provider.lock().await;
        cache.loaded = Instant::now() - KEY_TTL;
        cache.attempted = Instant::now() - REFRESH_INTERVAL;
    }
    provider.available(false);
    for _ in 0..3 {
        let response = reqwest::Client::new()
            .get(format!("{}/auth/oauth/google/authorize", running.http_url))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 503);
    }
    assert_eq!(provider.discoveries(), 2);
    provider.available(true);
    google.provider.lock().await.attempted = Instant::now() - REFRESH_INTERVAL;
    let (start, cookie) = running.begin().await;
    let (code, state) = provider
        .code(
            start["auth_url"].as_str().unwrap(),
            json!({"fixture_oversized":true}),
            1,
            1,
        )
        .await;
    assert_eq!(running.callback(&code, &state, &cookie).await.0, 503);
    assert_eq!(database.count("SELECT count(*) FROM users").await, 0);
    let (start, cookie) = running.begin().await;
    let (code, state) = provider
        .code(start["auth_url"].as_str().unwrap(), json!({}), 1, 1)
        .await;
    let capacity = google.exchanges.acquire_many(8).await.unwrap();
    let hits = provider.token_requests();
    assert_eq!(running.callback(&code, &state, &cookie).await.0, 429);
    assert_eq!(provider.token_requests(), hits);
    drop(capacity);
    assert_eq!(running.callback(&code, &state, &cookie).await.0, 200);
    running.close().await;
    database.clean().await;
}
