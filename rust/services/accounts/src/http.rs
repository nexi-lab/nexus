//! HTTP maps its wire requests to the canonical gRPC service methods.

use axum::{
    extract::{rejection::JsonRejection, DefaultBodyLimit, State},
    http::{header::CACHE_CONTROL, HeaderMap, HeaderValue, StatusCode},
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
        .route("/auth/login", post(login))
        .route("/auth/me", get(profile).patch(update_profile))
        .route("/auth/change-password", post(change_password))
        .route("/auth/request-verification", post(request_verification))
        .route("/auth/verify-email", post(verify_email))
        .layer(DefaultBodyLimit::max(32 * 1024))
        .layer(axum::middleware::map_response(no_store))
        .with_state(service)
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
