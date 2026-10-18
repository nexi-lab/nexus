use chrono::Utc;
use jsonwebtoken::{encode, Algorithm, EncodingKey, Header};
use nexus_accounts::{
    proto::{
        accounts_service_client::AccountsServiceClient,
        accounts_service_server::AccountsServiceServer, *,
    },
    store::AccountStore,
    AccountService,
};
use serde_json::{json, Value};
use sqlx::{sqlite::SqliteConnectOptions, SqlitePool};
use std::path::Path;
use tempfile::TempDir;
use tokio::{net::TcpListener, task::JoinHandle};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Code, Request};

const SECRET: &[u8] = b"fixture-account-signing-key-32-bytes-minimum";
const PASSWORD: &str = "old-password-for-test";

struct Fixture {
    temp: Option<TempDir>,
    pool: SqlitePool,
    store: AccountStore,
    service: AccountService,
    grpc: JoinHandle<()>,
    http: JoinHandle<()>,
    shutdown: tokio::sync::watch::Sender<bool>,
    grpc_url: String,
    http_url: String,
}

async fn seed(pool: &SqlitePool, id: &str, hash: &str) {
    sqlx::query("INSERT INTO users (user_id, email, username, display_name, password_hash, primary_auth_method, is_global_admin, is_active, email_verified, zone_id, created_at, updated_at, user_metadata) VALUES ($1, $2, $3, '既有账号', $4, 'password', 0, 1, 1, 'fixture-zone', $5, $5, '{\"preserved\":true}')")
        .bind(id).bind(format!("{id}@example.test")).bind(id).bind(hash)
        .bind(Utc::now().naive_utc()).execute(pool).await.unwrap();
}

async fn open_database(path: &Path) -> SqlitePool {
    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(path)
            .create_if_missing(true),
    )
    .await
    .unwrap();
    sqlx::raw_sql(include_str!("fixtures/users.sql"))
        .execute(&pool)
        .await
        .unwrap();
    let fixtures: Value = serde_json::from_str(include_str!("fixtures/passwords.json")).unwrap();
    seed(&pool, "alice", fixtures[0]["hash"].as_str().unwrap()).await;
    pool
}

impl Fixture {
    async fn new() -> Self {
        Self::with_email(None).await
    }

    async fn with_email(email: Option<nexus_accounts::email::VerificationEmail>) -> Self {
        let temp = tempfile::Builder::new()
            .prefix("nexus-account-test-")
            .tempdir()
            .unwrap();
        let path = temp.path().join("accounts.db");
        let pool = open_database(&path).await;
        let store = AccountStore::connect(&format!("sqlite:///{}", path.display()))
            .await
            .unwrap();
        let mut service = AccountService::new(store.clone(), SECRET, 3600, 2).unwrap();
        if let Some(email) = email {
            service = service.with_email_verification(email);
        }
        let owner_service = service.clone();
        let grpc_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let http_listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let grpc_url = format!("http://{}", grpc_listener.local_addr().unwrap());
        let http_url = format!("http://{}", http_listener.local_addr().unwrap());
        let (shutdown, _) = tokio::sync::watch::channel(false);
        let mut grpc_stop = shutdown.subscribe();
        let mut http_stop = shutdown.subscribe();
        let grpc_service = service.clone();
        let grpc = tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(AccountsServiceServer::new(grpc_service))
                .serve_with_incoming_shutdown(TcpListenerStream::new(grpc_listener), async move {
                    let _ = grpc_stop.changed().await;
                })
                .await
                .unwrap();
        });
        let http = tokio::spawn(async move {
            axum::serve(http_listener, nexus_accounts::http::router(service))
                .with_graceful_shutdown(async move {
                    let _ = http_stop.changed().await;
                })
                .await
                .unwrap();
        });
        Self {
            temp: Some(temp),
            pool,
            store,
            service: owner_service,
            grpc,
            http,
            shutdown,
            grpc_url,
            http_url,
        }
    }

    async fn client(&self) -> AccountsServiceClient<tonic::transport::Channel> {
        AccountsServiceClient::connect(self.grpc_url.clone())
            .await
            .unwrap()
    }

    async fn stop(mut self) {
        self.shutdown.send(true).unwrap();
        // Await by mutable reference because Drop also owns cancellation on panic.
        (&mut self.grpc).await.unwrap();
        (&mut self.http).await.unwrap();
        self.service.finish_email_requests().await;
        self.store.close().await;
        self.pool.close().await;
        self.temp.take().unwrap().close().unwrap();
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.grpc.abort();
        self.http.abort();
    }
}

fn authorized<T>(token: &str, body: T) -> Request<T> {
    let mut request = Request::new(body);
    request
        .metadata_mut()
        .insert("authorization", format!("Bearer {token}").parse().unwrap());
    request
}

fn legacy_claims() -> Value {
    json!({"sub":"alice", "subject_type":"user", "subject_id":"alice", "email":"alice@example.test", "iat":Utc::now().timestamp(), "exp":Utc::now().timestamp()+3600, "is_admin":true, "zone_id":"forged-zone"})
}

fn sign(claims: &Value, algorithm: Algorithm, secret: &[u8]) -> String {
    encode(
        &Header::new(algorithm),
        claims,
        &EncodingKey::from_secret(secret),
    )
    .unwrap()
}

fn verification_claims(id: &str, email: &str) -> Value {
    json!({"sub":id, "email":email, "purpose":"email_verify", "iat":Utc::now().timestamp(), "exp":Utc::now().timestamp()+86400})
}

#[tokio::test]
async fn registration_requires_verification_and_cannot_select_privilege_or_credentials() {
    let fixture = Fixture::new().await;
    let http = reqwest::Client::new();
    let endpoint = format!("{}/auth/register", fixture.http_url);
    let valid = json!({"email":"new@example.test", "password":PASSWORD, "username":"new"});
    let mut invalid: Vec<_> = [
        json!({"email":"not-an-email"}),
        json!({"password":"short"}),
        json!({"password":"密".repeat(7)}),
        json!({"username":""}),
        json!({"username":"alice@example.test"}),
        json!({"username":"x".repeat(256)}),
        json!({"display_name":"x".repeat(256)}),
    ]
    .into_iter()
    .map(|body| (body, 400))
    .collect();
    for field in [
        "user_id",
        "is_global_admin",
        "zone_id",
        "email_verified",
        "token",
        "api_key",
    ] {
        invalid.push((json!({(field):"forged"}), 422));
    }
    for (fields, expected) in invalid {
        let mut body = valid.clone();
        body.as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        let response = http.post(&endpoint).json(&body).send().await.unwrap();
        assert_eq!(response.status().as_u16(), expected);
    }
    let response = http.post(&endpoint).json(&valid).send().await.unwrap();
    assert_eq!(response.status().as_u16(), 201);
    assert_eq!(response.headers()["cache-control"], "no-store");
    let profile: Value = response.json().await.unwrap();
    let id = profile["user_id"].as_str().unwrap();
    assert_eq!(uuid::Uuid::parse_str(id).unwrap().get_version_num(), 4);
    assert_eq!(profile["email_verified"], false);
    assert_eq!(profile["is_global_admin"], false);
    assert_eq!(profile["display_name"], "new");
    assert!(profile["zone_id"].is_null());
    assert!(profile.get("token").is_none() && profile.get("api_key").is_none());
    let mut grpc = fixture.client().await;
    let login = LoginRequest {
        identifier: "new".into(),
        password: PASSWORD.into(),
    };
    assert_eq!(
        grpc.login(login.clone()).await.unwrap_err().code(),
        Code::PermissionDenied
    );
    let verification = sign(
        &verification_claims(id, "new@example.test"),
        Algorithm::HS256,
        SECRET,
    );
    grpc.verify_email(VerifyEmailRequest {
        token: verification,
    })
    .await
    .unwrap();
    let signed_in = grpc.login(login).await.unwrap().into_inner();
    assert_eq!(signed_in.user.unwrap().user_id, id);
    assert!(grpc
        .get_profile(authorized(&signed_in.token, GetProfileRequest {}))
        .await
        .is_ok());
    assert_eq!(
        http.post(&endpoint)
            .json(&valid)
            .send()
            .await
            .unwrap()
            .status()
            .as_u16(),
        409
    );

    let created = grpc
        .register(RegisterRequest {
            email: "grpc-new@example.test".into(),
            password: "密码改写回归通过".into(),
            username: None,
            display_name: Some("新账号 ✓".into()),
        })
        .await
        .unwrap()
        .into_inner();
    assert_ne!(created.user_id, id);
    assert_eq!(created.display_name.as_deref(), Some("新账号 ✓"));
    assert!(!created.email_verified && !created.is_global_admin);
    let hash: String = sqlx::query_scalar("SELECT password_hash FROM users WHERE user_id=$1")
        .bind(&created.user_id)
        .fetch_one(&fixture.pool)
        .await
        .unwrap();
    assert!(hash.starts_with("$2b$12$"));
    assert!(bcrypt::verify("密码改写回归通过", &hash).unwrap());
    fixture.stop().await;
}

#[tokio::test]
async fn smtp_failure_keeps_acceptance_uniform_and_does_not_verify_the_account() {
    // Reserve a port without listening, so connection refusal has no port race.
    let socket = tokio::net::TcpSocket::new_v4().unwrap();
    socket.bind("127.0.0.1:0".parse().unwrap()).unwrap();
    let email = nexus_accounts::email::VerificationEmail::new(
        &format!("smtp://{}", socket.local_addr().unwrap()),
        "nexus@example.test",
        "https://example.test/verify",
        None,
    )
    .unwrap();
    let fixture = Fixture::with_email(Some(email)).await;
    sqlx::query("UPDATE users SET email_verified=0 WHERE user_id='alice'")
        .execute(&fixture.pool)
        .await
        .unwrap();
    let mut grpc = fixture.client().await;
    let mut replies = Vec::new();
    for email in ["alice@example.test", "missing@example.test"] {
        replies.push(
            grpc.request_email_verification(RequestEmailVerificationRequest {
                email: Some(email.into()),
            })
            .await
            .unwrap()
            .into_inner(),
        );
        fixture.service.finish_email_requests().await;
    }
    assert_eq!(replies[0], replies[1]);
    assert_eq!(replies[0].message, "Verification request accepted.");
    let verified: i32 =
        sqlx::query_scalar("SELECT email_verified FROM users WHERE user_id='alice'")
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(verified, 0);
    fixture.stop().await;
}

#[tokio::test]
async fn verification_binds_current_identity_and_is_idempotent_across_transports() {
    let fixture = Fixture::new().await;
    sqlx::query("UPDATE users SET email_verified=0 WHERE user_id='alice'")
        .execute(&fixture.pool)
        .await
        .unwrap();
    let claims = verification_claims("alice", "alice@example.test");
    let mut grpc = fixture.client().await;
    let http = reqwest::Client::new();
    let token = sign(&claims, Algorithm::HS256, SECRET);
    let mut invalid: Vec<_> = [
        json!({"purpose":"account"}),
        json!({"sub":"bob"}),
        json!({"email":"stale@example.test"}),
        json!({"exp":1}),
        json!({"iat":Utc::now().timestamp()+120}),
        json!({"exp":Utc::now().timestamp()+90000}),
        json!({"aud":"other-service"}),
        json!({"nbf":Utc::now().timestamp()+120}),
    ]
    .into_iter()
    .map(|changes| {
        let mut changed = claims.clone();
        changed
            .as_object_mut()
            .unwrap()
            .extend(changes.as_object().unwrap().clone());
        sign(&changed, Algorithm::HS256, SECRET)
    })
    .collect();
    invalid.push(sign(&claims, Algorithm::HS384, SECRET));
    invalid.push(sign(
        &claims,
        Algorithm::HS256,
        b"different-signing-key-with-32-bytes",
    ));
    invalid.push(sign(&legacy_claims(), Algorithm::HS256, SECRET));
    for token in invalid {
        let error = grpc
            .verify_email(VerifyEmailRequest { token })
            .await
            .unwrap_err();
        assert_eq!(error.code(), Code::InvalidArgument);
    }
    for mutation in [
        "is_active=0",
        "deleted_at=CURRENT_TIMESTAMP",
        "email='changed@example.test'",
    ] {
        sqlx::query(&format!(
            "UPDATE users SET {mutation} WHERE user_id='alice'"
        ))
        .execute(&fixture.pool)
        .await
        .unwrap();
        assert_eq!(
            grpc.verify_email(VerifyEmailRequest {
                token: token.clone()
            })
            .await
            .unwrap_err()
            .code(),
            Code::InvalidArgument
        );
        sqlx::query("UPDATE users SET is_active=1, deleted_at=NULL, email='alice@example.test' WHERE user_id='alice'").execute(&fixture.pool).await.unwrap();
    }
    let request = LoginRequest {
        identifier: "alice".into(),
        password: PASSWORD.into(),
    };
    assert_eq!(
        grpc.login(request.clone()).await.unwrap_err().code(),
        Code::PermissionDenied
    );
    let response = http
        .post(format!("{}/auth/verify-email", fixture.http_url))
        .json(&json!({"token":token}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), 200);
    assert_eq!(response.headers()["cache-control"], "no-store");
    let updated: chrono::NaiveDateTime =
        sqlx::query_scalar("SELECT updated_at FROM users WHERE user_id='alice'")
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    grpc.verify_email(VerifyEmailRequest {
        token: token.clone(),
    })
    .await
    .unwrap();
    let replayed: chrono::NaiveDateTime =
        sqlx::query_scalar("SELECT updated_at FROM users WHERE user_id='alice'")
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(updated, replayed);
    assert_eq!(
        grpc.login(request)
            .await
            .unwrap()
            .into_inner()
            .user
            .unwrap()
            .user_id,
        "alice"
    );
    assert_eq!(
        http.get(format!("{}/auth/me", fixture.http_url))
            .bearer_auth(token)
            .send()
            .await
            .unwrap()
            .status(),
        401
    );
    fixture.stop().await;
}

#[tokio::test]
async fn concurrent_verification_preserves_the_unique_email_owner() {
    let fixture = Fixture::new().await;
    let fixtures: Value = serde_json::from_str(include_str!("fixtures/passwords.json")).unwrap();
    seed(&fixture.pool, "bob", fixtures[0]["hash"].as_str().unwrap()).await;
    sqlx::query("UPDATE users SET email_verified=0")
        .execute(&fixture.pool)
        .await
        .unwrap();
    sqlx::query("UPDATE users SET email='shared@example.test'")
        .execute(&fixture.pool)
        .await
        .unwrap();
    let mut alice = fixture.client().await;
    let mut bob = fixture.client().await;
    let (a, b) = tokio::join!(
        alice.verify_email(VerifyEmailRequest {
            token: sign(
                &verification_claims("alice", "shared@example.test"),
                Algorithm::HS256,
                SECRET
            )
        }),
        bob.verify_email(VerifyEmailRequest {
            token: sign(
                &verification_claims("bob", "shared@example.test"),
                Algorithm::HS256,
                SECRET
            )
        })
    );
    assert_eq!(a.is_ok() as usize + b.is_ok() as usize, 1);
    assert_eq!(
        a.err().or_else(|| b.err()).unwrap().code(),
        Code::AlreadyExists
    );
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM users WHERE email_verified=1")
        .fetch_one(&fixture.pool)
        .await
        .unwrap();
    assert_eq!(count, 1);
    fixture.stop().await;
}

#[tokio::test]
async fn absent_mail_configuration_has_the_same_failure_for_every_identity() {
    let fixture = Fixture::new().await;
    let mut grpc = fixture.client().await;
    for email in [
        None,
        Some("alice@example.test"),
        Some("missing@example.test"),
    ] {
        let error = grpc
            .request_email_verification(RequestEmailVerificationRequest {
                email: email.map(str::to_owned),
            })
            .await
            .unwrap_err();
        assert_eq!(error.code(), Code::Unavailable);
        assert_eq!(
            error.message(),
            "Email verification delivery is not configured"
        );
    }
    fixture.stop().await;
}

#[tokio::test]
async fn grpc_and_http_share_existing_identity_profile_and_password_facts() {
    let fixture = Fixture::new().await;
    let mut grpc = fixture.client().await;
    let session = grpc
        .login(LoginRequest {
            identifier: "alice".into(),
            password: PASSWORD.into(),
        })
        .await
        .unwrap()
        .into_inner();
    let user = session.user.unwrap();
    assert_eq!(user.user_id, "alice");
    assert_eq!(user.display_name.as_deref(), Some("既有账号"));
    assert_eq!(user.zone_id.as_deref(), Some("fixture-zone"));
    assert!(user.last_login_at.is_some());
    assert!(!user.is_global_admin);
    let (updated_at, last_login_at): (chrono::NaiveDateTime, chrono::NaiveDateTime) =
        sqlx::query_as("SELECT updated_at, last_login_at FROM users WHERE user_id='alice'")
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(updated_at, last_login_at);

    let http = reqwest::Client::new();
    let response = http
        .get(format!("{}/auth/me", fixture.http_url))
        .bearer_auth(&session.token)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();
    assert_eq!(response.headers()["cache-control"], "no-store");
    let profile: UserProfile = response.json().await.unwrap();
    assert_eq!(profile, user);
    let updated: UserProfile = http
        .patch(format!("{}/auth/me", fixture.http_url))
        .bearer_auth(&session.token)
        .json(
            &json!({"display_name":"跨协议资料 ✓", "avatar_url":"https://example.test/avatar.png"}),
        )
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let profile = grpc
        .get_profile(authorized(&session.token, GetProfileRequest {}))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(profile, updated);
    assert_eq!(profile.display_name.as_deref(), Some("跨协议资料 ✓"));
    grpc.change_password(authorized(
        &session.token,
        ChangePasswordRequest {
            current_password: PASSWORD.into(),
            new_password: "new-password-for-test".into(),
        },
    ))
    .await
    .unwrap();
    assert_eq!(
        grpc.login(LoginRequest {
            identifier: "alice@example.test".into(),
            password: PASSWORD.into()
        })
        .await
        .unwrap_err()
        .code(),
        Code::Unauthenticated
    );
    let session: LoginResponse = http
        .post(format!("{}/auth/login", fixture.http_url))
        .json(&json!({"identifier":"alice@example.test", "password":"new-password-for-test"}))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(session.user.unwrap().user_id, "alice");
    let stored: (String, String) =
        sqlx::query_as("SELECT password_hash, user_metadata FROM users WHERE user_id='alice'")
            .fetch_one(&fixture.pool)
            .await
            .unwrap();
    assert!(bcrypt::verify("new-password-for-test", &stored.0).unwrap());
    assert_eq!(stored.1, "{\"preserved\":true}");
    let tables: Vec<String> =
        sqlx::query_scalar("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            .fetch_all(&fixture.pool)
            .await
            .unwrap();
    assert_eq!(tables, ["users"]);
    fixture.stop().await;
}

#[tokio::test]
async fn python_bcrypt_hashes_accept_multibyte_truncation_and_embedded_nul() {
    let fixture = Fixture::new().await;
    let passwords: Value = serde_json::from_str(include_str!("fixtures/passwords.json")).unwrap();
    let mut grpc = fixture.client().await;
    for (index, password) in passwords.as_array().unwrap().iter().enumerate() {
        let id = format!("compat-{index}");
        seed(&fixture.pool, &id, password["hash"].as_str().unwrap()).await;
        let session = grpc
            .login(LoginRequest {
                identifier: id.clone(),
                password: password["password"].as_str().unwrap().into(),
            })
            .await
            .unwrap()
            .into_inner();
        assert_eq!(session.user.unwrap().user_id, id);
    }
    fixture.stop().await;
}

#[tokio::test]
async fn legacy_jwt_is_scoped_to_its_database_identity_and_failures_match_on_both_transports() {
    let fixture = Fixture::new().await;
    let mut grpc = fixture.client().await;
    let http = reqwest::Client::new();
    let claims = legacy_claims();
    let token = sign(&claims, Algorithm::HS256, SECRET);
    let profile = grpc
        .get_profile(authorized(&token, GetProfileRequest {}))
        .await
        .unwrap()
        .into_inner();
    assert!(!profile.is_global_admin);
    assert_eq!(profile.zone_id.as_deref(), Some("fixture-zone"));
    let mut bad_tokens = vec![
        format!("{token}tampered"),
        sign(&claims, Algorithm::HS512, SECRET),
        sign(
            &claims,
            Algorithm::HS256,
            b"other-fixture-signing-secret-32-bytes",
        ),
    ];
    for change in [
        json!({"exp":1}),
        json!({"subject_id":"bob"}),
        json!({"subject_type":"agent"}),
        json!({"email":"other@example.test"}),
        json!({"purpose":"email-verification"}),
        json!({"aud":"other-service"}),
    ] {
        let mut changed = claims.clone();
        changed
            .as_object_mut()
            .unwrap()
            .extend(change.as_object().unwrap().clone());
        bad_tokens.push(sign(&changed, Algorithm::HS256, SECRET));
    }
    for token in bad_tokens {
        assert_eq!(
            grpc.get_profile(authorized(&token, GetProfileRequest {}))
                .await
                .unwrap_err()
                .code(),
            Code::Unauthenticated
        );
        assert_eq!(
            http.get(format!("{}/auth/me", fixture.http_url))
                .bearer_auth(&token)
                .send()
                .await
                .unwrap()
                .status(),
            reqwest::StatusCode::UNAUTHORIZED
        );
    }
    assert_eq!(
        grpc.get_profile(GetProfileRequest {})
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    sqlx::query("UPDATE users SET is_active=0 WHERE user_id='alice'")
        .execute(&fixture.pool)
        .await
        .unwrap();
    assert_eq!(
        grpc.get_profile(authorized(&token, GetProfileRequest {}))
            .await
            .unwrap_err()
            .code(),
        Code::Unauthenticated
    );
    assert_eq!(
        grpc.login(LoginRequest {
            identifier: "alice".into(),
            password: PASSWORD.into()
        })
        .await
        .unwrap_err()
        .code(),
        Code::Unauthenticated
    );
    fixture.stop().await;
}

#[tokio::test]
async fn invalid_mutations_preserve_existing_password_and_profile() {
    let fixture = Fixture::new().await;
    let mut grpc = fixture.client().await;
    let token = sign(&legacy_claims(), Algorithm::HS256, SECRET);
    for (old, new, expected) in [
        (
            "wrong-password",
            "valid-new-password",
            Code::Unauthenticated,
        ),
        (PASSWORD, "short", Code::InvalidArgument),
    ] {
        assert_eq!(
            grpc.change_password(authorized(
                &token,
                ChangePasswordRequest {
                    current_password: old.into(),
                    new_password: new.into()
                }
            ))
            .await
            .unwrap_err()
            .code(),
            expected
        );
    }
    let response = reqwest::Client::new()
        .patch(format!("{}/auth/me", fixture.http_url))
        .bearer_auth(&token)
        .json(&json!({"is_global_admin":true}))
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), reqwest::StatusCode::UNPROCESSABLE_ENTITY);
    assert_eq!(response.headers()["cache-control"], "no-store");
    grpc.login(LoginRequest {
        identifier: "alice".into(),
        password: PASSWORD.into(),
    })
    .await
    .unwrap();
    sqlx::query("UPDATE users SET email_verified=0 WHERE user_id='alice'")
        .execute(&fixture.pool)
        .await
        .unwrap();
    assert_eq!(
        grpc.login(LoginRequest {
            identifier: "alice".into(),
            password: PASSWORD.into()
        })
        .await
        .unwrap_err()
        .code(),
        Code::PermissionDenied
    );
    fixture.stop().await;
}

#[tokio::test]
async fn missing_database_and_missing_account_schema_fail_without_initializing_state() {
    let dir = tempfile::Builder::new()
        .prefix("nexus-account-missing-")
        .tempdir()
        .unwrap();
    let missing = dir.path().join("missing.db");
    assert!(
        AccountStore::connect(&format!("sqlite:///{}", missing.display()))
            .await
            .is_err()
    );
    assert!(!missing.exists());
    let empty = dir.path().join("empty.db");
    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(&empty)
            .create_if_missing(true),
    )
    .await
    .unwrap();
    assert!(
        AccountStore::connect(&format!("sqlite:///{}", empty.display()))
            .await
            .is_err()
    );
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
        .fetch_one(&pool)
        .await
        .unwrap();
    assert_eq!(count, 0);
    pool.close().await;
    dir.close().unwrap();
}

#[tokio::test]
async fn ambiguous_identifiers_are_rejected_and_storage_failures_are_visible() {
    let fixture = Fixture::new().await;
    let passwords: Value = serde_json::from_str(include_str!("fixtures/passwords.json")).unwrap();
    seed(
        &fixture.pool,
        "another",
        passwords[0]["hash"].as_str().unwrap(),
    )
    .await;
    sqlx::query("UPDATE users SET username='alice' WHERE user_id='another'")
        .execute(&fixture.pool)
        .await
        .unwrap();
    let mut grpc = fixture.client().await;
    assert_eq!(
        grpc.login(LoginRequest {
            identifier: "alice".into(),
            password: PASSWORD.into()
        })
        .await
        .unwrap_err()
        .code(),
        Code::Unauthenticated
    );
    let token = sign(&legacy_claims(), Algorithm::HS256, SECRET);
    sqlx::query("ALTER TABLE users RENAME TO unavailable_accounts")
        .execute(&fixture.pool)
        .await
        .unwrap();
    assert_eq!(
        grpc.get_profile(authorized(&token, GetProfileRequest {}))
            .await
            .unwrap_err()
            .code(),
        Code::Unavailable
    );
    let response = reqwest::Client::new()
        .get(format!("{}/auth/me", fixture.http_url))
        .bearer_auth(&token)
        .send()
        .await
        .unwrap();
    assert_eq!(response.status(), reqwest::StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(response.headers()["cache-control"], "no-store");
    assert_eq!(
        response.json::<Value>().await.unwrap()["detail"],
        "Account database is unavailable"
    );
    sqlx::query("ALTER TABLE unavailable_accounts RENAME TO users")
        .execute(&fixture.pool)
        .await
        .unwrap();
    assert_eq!(
        grpc.get_profile(authorized(&token, GetProfileRequest {}))
            .await
            .unwrap()
            .into_inner()
            .user_id,
        "alice"
    );
    fixture.stop().await;
}
