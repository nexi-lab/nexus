//! HTTP maps its wire requests to the canonical gRPC service methods.

use axum::{
    body::Bytes,
    extract::{rejection::JsonRejection, DefaultBodyLimit, RawQuery, State},
    http::{
        header::{CACHE_CONTROL, COOKIE, SET_COOKIE},
        HeaderMap, HeaderValue, StatusCode,
    },
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::json;
use tonic::{Code, Request, Status};

use crate::{
    proto::{accounts_service_server::AccountsService, *},
    AccountService,
};

pub fn router(service: AccountService) -> Router {
    Router::new()
        .route("/auth/register", post(register))
        .route("/auth/login", post(login))
        .route(
            "/auth/me",
            get(profile).patch(update_profile).delete(delete_account),
        )
        .route("/auth/change-password", post(change_password))
        .route("/auth/request-verification", post(request_verification))
        .route("/auth/verify-email", post(verify_email))
        .route("/auth/oauth/google/authorize", get(google_authorize))
        .route("/auth/oauth/google/callback", post(google_callback))
        .layer(DefaultBodyLimit::max(32 * 1024))
        .layer(axum::middleware::map_response(no_store))
        .with_state(service)
}

fn google_cookie(service: &AccountService, value: &str, age: u64) -> Result<String, Status> {
    let secure = service.google()?.secure_cookie();
    let name = if secure {
        "__Host-nexus_google"
    } else {
        "nexus_google"
    };
    Ok(format!(
        "{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{}",
        if secure { "; Secure" } else { "" }
    ))
}

async fn google_authorize(State(service): State<AccountService>) -> Result<Response, AccountError> {
    let result = service
        .start_google_login(Request::new(StartGoogleLoginRequest {}))
        .await?
        .into_inner();
    let cookie = google_cookie(
        &service,
        &result.browser_binding,
        crate::oauth::FLOW_SECONDS,
    )?;
    Ok(([(SET_COOKIE, cookie)], Json(result)).into_response())
}

async fn google_callback(
    State(service): State<AccountService>,
    headers: HeaderMap,
    body: Result<Json<FinishGoogleLoginRequest>, JsonRejection>,
) -> Response {
    let result = async {
        let Json(mut body) = body.map_err(AccountError::Body)?;
        let name = if service.google()?.secure_cookie() {
            "__Host-nexus_google"
        } else {
            "nexus_google"
        };
        let mut values = headers
            .get_all(COOKIE)
            .iter()
            .filter_map(|header| header.to_str().ok())
            .flat_map(|header| header.split(';'))
            .filter_map(|cookie| cookie.trim().split_once('='))
            .filter(|(key, _)| *key == name)
            .map(|(_, value)| value);
        let binding = values
            .next()
            .ok_or_else(|| Status::unauthenticated("Google browser binding is required"))?;
        if values.next().is_some() {
            return Err(Status::unauthenticated("Ambiguous Google browser binding").into());
        }
        body.browser_binding = binding.into();
        let response = service.finish_google_login(Request::new(body)).await?;
        Ok::<_, AccountError>(Json(response.into_inner()))
    }
    .await;
    let mut response = result.into_response();
    if let Ok(cookie) = google_cookie(&service, "", 0) {
        if let Ok(cookie) = HeaderValue::from_str(&cookie) {
            response.headers_mut().insert(SET_COOKIE, cookie);
        }
    }
    response
}

async fn register(
    State(service): State<AccountService>,
    body: Result<Json<RegisterRequest>, JsonRejection>,
) -> Result<(StatusCode, Json<UserProfile>), AccountError> {
    let Json(body) = body.map_err(AccountError::Body)?;
    Ok((
        StatusCode::CREATED,
        Json(service.register(Request::new(body)).await?.into_inner()),
    ))
}

async fn no_store(mut response: Response) -> Response {
    response
        .headers_mut()
        .insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    response
}

enum AccountError {
    Rpc(Status),
    Body(JsonRejection),
}

impl From<Status> for AccountError {
    fn from(status: Status) -> Self {
        Self::Rpc(status)
    }
}

impl IntoResponse for AccountError {
    fn into_response(self) -> Response {
        let status = match self {
            Self::Rpc(status) => status,
            Self::Body(rejection) => {
                return (
                    rejection.status(),
                    Json(json!({"detail": rejection.body_text()})),
                )
                    .into_response();
            }
        };
        let code = match status.code() {
            Code::InvalidArgument => StatusCode::BAD_REQUEST,
            Code::Unauthenticated => StatusCode::UNAUTHORIZED,
            Code::PermissionDenied => StatusCode::FORBIDDEN,
            Code::NotFound => StatusCode::NOT_FOUND,
            Code::Aborted => StatusCode::CONFLICT,
            Code::AlreadyExists => StatusCode::CONFLICT,
            Code::FailedPrecondition => StatusCode::CONFLICT,
            Code::ResourceExhausted => StatusCode::TOO_MANY_REQUESTS,
            Code::Unavailable => StatusCode::SERVICE_UNAVAILABLE,
            _ => StatusCode::INTERNAL_SERVER_ERROR,
        };
        (code, Json(json!({"detail": status.message()}))).into_response()
    }
}

async fn request_verification(
    State(service): State<AccountService>,
    body: Result<Json<RequestEmailVerificationRequest>, JsonRejection>,
) -> Result<(StatusCode, Json<AccountMessage>), AccountError> {
    let Json(body) = body.map_err(AccountError::Body)?;
    Ok((
        StatusCode::ACCEPTED,
        Json(
            service
                .request_email_verification(Request::new(body))
                .await?
                .into_inner(),
        ),
    ))
}

async fn verify_email(
    State(service): State<AccountService>,
    body: Result<Json<VerifyEmailRequest>, JsonRejection>,
) -> Result<Json<AccountMessage>, AccountError> {
    let Json(body) = body.map_err(AccountError::Body)?;
    Ok(Json(
        service.verify_email(Request::new(body)).await?.into_inner(),
    ))
}

fn authorized<T>(headers: &HeaderMap, body: T) -> Result<Request<T>, AccountError> {
    let bearer = headers
        .get("authorization")
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| Status::unauthenticated("Account token is required"))?;
    let mut request = Request::new(body);
    request.metadata_mut().insert(
        "authorization",
        bearer
            .parse()
            .map_err(|_| Status::unauthenticated("Invalid account token"))?,
    );
    Ok(request)
}

async fn login(
    State(service): State<AccountService>,
    body: Result<Json<LoginRequest>, JsonRejection>,
) -> Result<Json<LoginResponse>, AccountError> {
    let Json(body) = body.map_err(AccountError::Body)?;
    Ok(Json(service.login(Request::new(body)).await?.into_inner()))
}

async fn profile(
    State(service): State<AccountService>,
    headers: HeaderMap,
) -> Result<Json<UserProfile>, AccountError> {
    Ok(Json(
        service
            .get_profile(authorized(&headers, GetProfileRequest {})?)
            .await?
            .into_inner(),
    ))
}

async fn delete_account(
    State(service): State<AccountService>,
    headers: HeaderMap,
    RawQuery(query): RawQuery,
    body: Bytes,
) -> Result<Json<AccountMessage>, AccountError> {
    if query.is_some() || !body.is_empty() {
        return Err(
            Status::invalid_argument("Account deletion takes no body or query parameters").into(),
        );
    }
    Ok(Json(
        service
            .delete_account(authorized(&headers, DeleteAccountRequest {})?)
            .await?
            .into_inner(),
    ))
}

async fn update_profile(
    State(service): State<AccountService>,
    headers: HeaderMap,
    body: Result<Json<UpdateProfileRequest>, JsonRejection>,
) -> Result<Json<UserProfile>, AccountError> {
    let Json(body) = body.map_err(AccountError::Body)?;
    Ok(Json(
        service
            .update_profile(authorized(&headers, body)?)
            .await?
            .into_inner(),
    ))
}

async fn change_password(
    State(service): State<AccountService>,
    headers: HeaderMap,
    body: Result<Json<ChangePasswordRequest>, JsonRejection>,
) -> Result<Json<ChangePasswordResponse>, AccountError> {
    let Json(body) = body.map_err(AccountError::Body)?;
    Ok(Json(
        service
            .change_password(authorized(&headers, body)?)
            .await?
            .into_inner(),
    ))
}
