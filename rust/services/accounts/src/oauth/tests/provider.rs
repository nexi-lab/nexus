//! A loopback authorization server. Its codes are single use and enforce PKCE.

use super::*;
use axum::{
    extract::{Form, Query, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::atomic::{AtomicBool, AtomicUsize, Ordering},
};

#[derive(Clone)]
struct Code {
    challenge: String,
    redirect: String,
    nonce: String,
    claims: Value,
    signing_key: usize,
    header_key: usize,
}

struct StateData {
    issuer: String,
    codes: Mutex<HashMap<String, Code>>,
    jwks: Mutex<Value>,
    pub token_requests: AtomicUsize,
    pub discoveries: AtomicUsize,
    healthy: AtomicBool,
}

pub(super) struct ProviderFixture {
    pub issuer: String,
    state: Arc<StateData>,
    task: tokio::task::JoinHandle<()>,
}

impl ProviderFixture {
    pub async fn new() -> Self {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let issuer = format!("http://{}", listener.local_addr().unwrap());
        let state = Arc::new(StateData {
            issuer: issuer.clone(),
            codes: Mutex::new(HashMap::new()),
            jwks: Mutex::new(
                json!({"keys":[serde_json::from_str::<Value>(include_str!("../../../tests/fixtures/google-test-jwk-1.json")).unwrap()]}),
            ),
            token_requests: AtomicUsize::new(0),
            discoveries: AtomicUsize::new(0),
            healthy: AtomicBool::new(true),
        });
        let app = Router::new()
            .route("/.well-known/openid-configuration", get(discover))
            .route("/jwks", get(jwks))
            .route("/authorize", get(authorize))
            .route("/token", post(token))
            .with_state(Arc::clone(&state));
        let task = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        Self {
            issuer,
            state,
            task,
        }
    }

    pub fn token_requests(&self) -> usize {
        self.state.token_requests.load(Ordering::SeqCst)
    }
    pub fn discoveries(&self) -> usize {
        self.state.discoveries.load(Ordering::SeqCst)
    }

    pub fn available(&self, available: bool) {
        self.state.healthy.store(available, Ordering::SeqCst);
    }

    pub async fn rotate(&self) {
        *self.state.jwks.lock().await = json!({"keys":[serde_json::from_str::<Value>(include_str!("../../../tests/fixtures/google-test-jwk-2.json")).unwrap()]});
    }

    pub async fn code(
        &self,
        auth_url: &str,
        claims: Value,
        signing_key: usize,
        header_key: usize,
    ) -> (String, String) {
        let mut url = url::Url::parse(auth_url).unwrap();
        url.query_pairs_mut()
            .append_pair("fixture_claims", &claims.to_string())
            .append_pair("fixture_signing_key", &signing_key.to_string())
            .append_pair("fixture_header_key", &header_key.to_string());
        let response = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .build()
            .unwrap()
            .get(url)
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::FOUND);
        let callback = url::Url::parse(response.headers()["location"].to_str().unwrap()).unwrap();
        let params: HashMap<_, _> = callback.query_pairs().into_owned().collect();
        (params["code"].clone(), params["state"].clone())
    }
}

impl Drop for ProviderFixture {
    fn drop(&mut self) {
        self.task.abort();
    }
}

async fn discover(State(state): State<Arc<StateData>>) -> Response {
    state.discoveries.fetch_add(1, Ordering::SeqCst);
    if !state.healthy.load(Ordering::SeqCst) {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    Json(
        json!({"issuer":state.issuer, "authorization_endpoint":format!("{}/authorize",state.issuer),
        "token_endpoint":format!("{}/token",state.issuer), "jwks_uri":format!("{}/jwks",state.issuer),
        "response_types_supported":["code"], "subject_types_supported":["public"], "id_token_signing_alg_values_supported":["RS256"]}),
    ).into_response()
}

async fn jwks(State(state): State<Arc<StateData>>) -> Json<Value> {
    Json(state.jwks.lock().await.clone())
}

async fn authorize(
    State(state): State<Arc<StateData>>,
    Query(params): Query<HashMap<String, String>>,
) -> Response {
    assert_eq!(params["client_id"], "fixture-client");
    assert_eq!(params["response_type"], "code");
    assert_eq!(params["code_challenge_method"], "S256");
    assert!(params["scope"].split_whitespace().any(|s| s == "openid"));
    let code = uuid::Uuid::new_v4().to_string();
    state.codes.lock().await.insert(
        code.clone(),
        Code {
            challenge: params["code_challenge"].clone(),
            redirect: params["redirect_uri"].clone(),
            nonce: params["nonce"].clone(),
            claims: serde_json::from_str(&params["fixture_claims"]).unwrap(),
            signing_key: params["fixture_signing_key"].parse().unwrap(),
            header_key: params["fixture_header_key"].parse().unwrap(),
        },
    );
    let mut callback = url::Url::parse(&params["redirect_uri"]).unwrap();
    callback
        .query_pairs_mut()
        .append_pair("code", &code)
        .append_pair("state", &params["state"]);
    (StatusCode::FOUND, [("location", callback.as_str())]).into_response()
}

async fn token(
    State(state): State<Arc<StateData>>,
    Form(params): Form<HashMap<String, String>>,
) -> Response {
    state.token_requests.fetch_add(1, Ordering::SeqCst);
    let invalid = || {
        (
            StatusCode::BAD_REQUEST,
            Json(json!({"error":"invalid_grant"})),
        )
            .into_response()
    };
    let Some(code) = state
        .codes
        .lock()
        .await
        .remove(params.get("code").map(String::as_str).unwrap_or_default())
    else {
        return invalid();
    };
    if params.get("client_id").map(String::as_str) != Some("fixture-client")
        || params.get("client_secret").map(String::as_str) != Some("fixture-client-secret")
        || params.get("redirect_uri") != Some(&code.redirect)
        || params.get("grant_type").map(String::as_str) != Some("authorization_code")
        || params.get("code_verifier").is_none_or(|v| {
            PkceCodeChallenge::from_code_verifier_sha256(&PkceCodeVerifier::new(v.clone())).as_str()
                != code.challenge
        })
    {
        return invalid();
    }
    let now = Utc::now().timestamp();
    if code.claims.get("fixture_oversized") == Some(&Value::Bool(true)) {
        return Json(json!({"padding":"x".repeat(129 * 1024)})).into_response();
    }
    let mut claims = json!({"iss":state.issuer,"aud":"fixture-client","sub":"google-subject","email":"google@example.test","email_verified":true,"at_hash":"M5dHLa8Ai7hmudQmSKqjlg",
        "name":"Google User","picture":"https://example.test/avatar.png","nonce":code.nonce,"iat":now,"exp":now+3600});
    for (key, value) in code.claims.as_object().unwrap() {
        if value.is_null() {
            claims.as_object_mut().unwrap().remove(key);
        } else {
            claims[key] = value.clone();
        }
    }
    let mut header = Header::new(if code.signing_key == 0 {
        Algorithm::HS256
    } else {
        Algorithm::RS256
    });
    header.kid = Some(format!("fixture-{}", code.header_key));
    let key = match code.signing_key {
        0 => EncodingKey::from_secret(b"fixture-client-secret"),
        1 => EncodingKey::from_rsa_pem(include_bytes!(
            "../../../tests/fixtures/google-test-key-1.pem"
        ))
        .unwrap(),
        _ => EncodingKey::from_rsa_pem(include_bytes!(
            "../../../tests/fixtures/google-test-key-2.pem"
        ))
        .unwrap(),
    };
    let token = encode(&header, &claims, &key).unwrap();
    Json(json!({"access_token":"fixture-provider-access-token","token_type":"Bearer","id_token":token,"expires_in":3600})).into_response()
}
