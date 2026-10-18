//! Google authenticates an external subject; SQL owns its durable account binding.

use std::{future::Future, pin::Pin, sync::Arc, time::Duration};

use chrono::Utc;
use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use openidconnect::{
    core::{
        CoreAuthDisplay, CoreAuthPrompt, CoreAuthenticationFlow, CoreErrorResponseType,
        CoreGenderClaim, CoreJsonWebKey, CoreJweContentEncryptionAlgorithm,
        CoreJwsSigningAlgorithm, CoreProviderMetadata, CoreRevocableToken,
        CoreRevocationErrorResponse, CoreTokenIntrospectionResponse, CoreTokenType,
    },
    reqwest, AccessTokenHash, AdditionalClaims, AsyncHttpClient, AuthType, AuthorizationCode,
    Client, ClientId, ClientSecret, CsrfToken, EmptyExtraTokenFields, EndpointMaybeSet,
    EndpointNotSet, EndpointSet, HttpRequest, HttpResponse, IdTokenFields, IssuerUrl, Nonce,
    OAuth2TokenResponse, PkceCodeChallenge, PkceCodeVerifier, RedirectUrl, Scope,
    SignatureVerificationError, StandardErrorResponse, StandardTokenResponse, TokenResponse,
};
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use tokio::{sync::Mutex, time::Instant};
use tonic::Status;

use crate::proto::{FinishGoogleLoginRequest, StartGoogleLoginResponse};

const GOOGLE_ISSUER: &str = "https://accounts.google.com";
pub(crate) const FLOW_SECONDS: u64 = 600;
const KEY_TTL: Duration = Duration::from_secs(300);
const REFRESH_INTERVAL: Duration = Duration::from_secs(30);

#[derive(Clone, Debug, Deserialize, Serialize)]
struct GoogleClaims {
    #[serde(default)]
    nbf: Option<i64>,
}

impl AdditionalClaims for GoogleClaims {}

type GoogleToken = StandardTokenResponse<
    IdTokenFields<
        GoogleClaims,
        EmptyExtraTokenFields,
        CoreGenderClaim,
        CoreJweContentEncryptionAlgorithm,
        CoreJwsSigningAlgorithm,
    >,
    CoreTokenType,
>;

type OidcClient = Client<
    GoogleClaims,
    CoreAuthDisplay,
    CoreGenderClaim,
    CoreJweContentEncryptionAlgorithm,
    CoreJsonWebKey,
    CoreAuthPrompt,
    StandardErrorResponse<CoreErrorResponseType>,
    GoogleToken,
    CoreTokenIntrospectionResponse,
    CoreRevocableToken,
    CoreRevocationErrorResponse,
    EndpointSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointNotSet,
    EndpointMaybeSet,
    EndpointMaybeSet,
>;

struct Provider {
    client: Arc<OidcClient>,
    loaded: Instant,
    attempted: Instant,
}

pub struct GoogleLogin {
    issuer: IssuerUrl,
    client_id: ClientId,
    client_secret: ClientSecret,
    redirect: RedirectUrl,
    http: ProviderHttp,
    provider: Mutex<Provider>,
    encoding: EncodingKey,
    decoding: DecodingKey,
    validation: Validation,
    exchanges: tokio::sync::Semaphore,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Flow {
    sub: String,
    purpose: String,
    challenge: String,
    client_id: String,
    redirect_uri: String,
    iat: u64,
    exp: u64,
}

/// Constructed only after signature, issuer, audience, nonce and email verification.
pub(crate) struct GoogleIdentity {
    pub subject: String,
    pub email: String,
    pub name: Option<String>,
    pub picture: Option<String>,
}

fn invalid() -> Status {
    Status::unauthenticated("Invalid or expired Google sign-in")
}

/// The OIDC client's default HTTP adapter buffers arbitrary bodies. Bound both
/// time and bytes here and never follow provider redirects with credentials.
struct ProviderHttp(reqwest::Client);

impl<'c> AsyncHttpClient<'c> for ProviderHttp {
    type Error = std::io::Error;
    type Future =
        Pin<Box<dyn Future<Output = Result<HttpResponse, Self::Error>> + Send + Sync + 'c>>;

    fn call(&'c self, request: HttpRequest) -> Self::Future {
        Box::pin(async move {
            let mut response = self
                .0
                .execute(request.try_into().map_err(std::io::Error::other)?)
                .await
                .map_err(std::io::Error::other)?;
            let mut builder = openidconnect::http::Response::builder().status(response.status());
            for (name, value) in response.headers() {
                builder = builder.header(name, value);
            }
            let mut body = Vec::new();
            while let Some(chunk) = response.chunk().await.map_err(std::io::Error::other)? {
                if body.len() + chunk.len() > 128 * 1024 {
                    return Err(std::io::Error::other("OIDC response is too large"));
                }
                body.extend_from_slice(&chunk);
            }
            builder.body(body).map_err(std::io::Error::other)
        })
    }
}

impl GoogleLogin {
    pub async fn new(
        client_id: String,
        client_secret: String,
        redirect_uri: String,
        jwt_secret: &[u8],
    ) -> Result<Self, Status> {
        let http = reqwest::Client::builder()
            .https_only(true)
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(3))
            .timeout(Duration::from_secs(5))
            .build()
            .map_err(|_| Status::internal("Could not configure Google connection"))?;
        Self::configured(
            client_id,
            client_secret,
            redirect_uri,
            jwt_secret,
            IssuerUrl::new(GOOGLE_ISSUER.into()).expect("constant Google issuer"),
            ProviderHttp(http),
        )
        .await
    }

    async fn configured(
        client_id: String,
        client_secret: String,
        redirect_uri: String,
        jwt_secret: &[u8],
        issuer: IssuerUrl,
        http: ProviderHttp,
    ) -> Result<Self, Status> {
        let bad_config = || Status::invalid_argument("Invalid Google sign-in configuration");
        let redirect = RedirectUrl::new(redirect_uri).map_err(|_| bad_config())?;
        let url = redirect.url();
        let loopback = url.host_str().is_some_and(|host| {
            host == "localhost"
                || host
                    .trim_matches(['[', ']'])
                    .parse::<std::net::IpAddr>()
                    .is_ok_and(|ip| ip.is_loopback())
        });
        if client_id.is_empty()
            || client_id.len() > 1024
            || client_secret.is_empty()
            || client_secret.len() > 4096
            || jwt_secret.len() < 32
            || url.host_str().is_none()
            || !url.username().is_empty()
            || url.password().is_some()
            || url.fragment().is_some()
            || url.query().is_some()
            || !(url.scheme() == "https" || (url.scheme() == "http" && loopback))
        {
            return Err(bad_config());
        }
        let client_id = ClientId::new(client_id);
        let client_secret = ClientSecret::new(client_secret);
        let client = Self::discover(&issuer, &client_id, &client_secret, &redirect, &http).await?;
        let mut validation = Validation::new(Algorithm::HS256);
        validation.leeway = 0;
        validation.validate_nbf = true;
        validation.set_required_spec_claims(&["exp", "sub"]);
        Ok(Self {
            issuer,
            client_id,
            client_secret,
            redirect,
            http,
            provider: Mutex::new(Provider {
                client: Arc::new(client),
                loaded: Instant::now(),
                attempted: Instant::now() - REFRESH_INTERVAL,
            }),
            encoding: EncodingKey::from_secret(jwt_secret),
            decoding: DecodingKey::from_secret(jwt_secret),
            validation,
            exchanges: tokio::sync::Semaphore::new(8),
        })
    }

    async fn discover(
        issuer: &IssuerUrl,
        id: &ClientId,
        secret: &ClientSecret,
        redirect: &RedirectUrl,
        http: &ProviderHttp,
    ) -> Result<OidcClient, Status> {
        let metadata = CoreProviderMetadata::discover_async(issuer.clone(), http)
            .await
            .map_err(|_| Status::unavailable("Google identity provider is unavailable"))?;
        if metadata.token_endpoint().is_none() {
            return Err(Status::unavailable("Google token endpoint is unavailable"));
        }
        if issuer.url().scheme() == "https"
            && [
                metadata.authorization_endpoint().url(),
                metadata.jwks_uri().url(),
                metadata
                    .token_endpoint()
                    .expect("checked token endpoint")
                    .url(),
            ]
            .iter()
            .any(|url| url.scheme() != "https")
        {
            return Err(Status::unavailable("Google endpoints require HTTPS"));
        }
        Ok(
            Client::from_provider_metadata(metadata, id.clone(), Some(secret.clone()))
                .set_auth_type(AuthType::RequestBody)
                .set_redirect_uri(redirect.clone()),
        )
    }

    async fn client(&self, key_miss: bool) -> Result<Arc<OidcClient>, Status> {
        let mut provider = self.provider.lock().await;
        if !key_miss && provider.loaded.elapsed() < KEY_TTL {
            return Ok(Arc::clone(&provider.client));
        }
        if provider.attempted.elapsed() < REFRESH_INTERVAL {
            return if provider.loaded.elapsed() < KEY_TTL {
                Ok(Arc::clone(&provider.client))
            } else {
                Err(Status::unavailable(
                    "Google identity provider is unavailable",
                ))
            };
        }
        provider.attempted = Instant::now();
        let client = Self::discover(
            &self.issuer,
            &self.client_id,
            &self.client_secret,
            &self.redirect,
            &self.http,
        )
        .await?;
        provider.client = Arc::new(client);
        provider.loaded = Instant::now();
        Ok(Arc::clone(&provider.client))
    }

    pub(crate) fn secure_cookie(&self) -> bool {
        self.redirect.url().scheme() == "https"
    }

    pub(crate) async fn start(&self) -> Result<StartGoogleLoginResponse, Status> {
        let (challenge, verifier) = PkceCodeChallenge::new_random_sha256();
        let nonce = Nonce::new_random();
        let now = Utc::now().timestamp() as u64;
        let state = encode(
            &Header::new(Algorithm::HS256),
            &Flow {
                sub: nonce.secret().clone(),
                purpose: "google_login".into(),
                challenge: challenge.as_str().into(),
                client_id: self.client_id.as_str().into(),
                redirect_uri: self.redirect.as_str().into(),
                iat: now,
                exp: now + FLOW_SECONDS,
            },
            &self.encoding,
        )
        .map_err(|_| Status::internal("Could not start Google sign-in"))?;
        let client = self.client(false).await?;
        let csrf = CsrfToken::new(state.clone());
        let (auth_url, _, _) = client
            .authorize_url(
                CoreAuthenticationFlow::AuthorizationCode,
                move || csrf,
                move || nonce,
            )
            .add_scope(Scope::new("email".into()))
            .add_scope(Scope::new("profile".into()))
            .set_pkce_challenge(challenge)
            .url();
        Ok(StartGoogleLoginResponse {
            auth_url: auth_url.into(),
            state,
            browser_binding: verifier.secret().clone(),
        })
    }

    pub(crate) async fn finish(
        &self,
        request: FinishGoogleLoginRequest,
    ) -> Result<GoogleIdentity, Status> {
        if request.code.is_empty()
            || request.code.len() > 8192
            || request.state.len() > 4096
            || request.browser_binding.len() != 43
            || !request
                .browser_binding
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
        {
            return Err(invalid());
        }
        let flow = decode::<Flow>(&request.state, &self.decoding, &self.validation)
            .map_err(|_| invalid())?
            .claims;
        let now = Utc::now().timestamp() as u64;
        let verifier = PkceCodeVerifier::new(request.browser_binding);
        let challenge = PkceCodeChallenge::from_code_verifier_sha256(&verifier);
        if flow.purpose != "google_login"
            || flow.sub.is_empty()
            || flow.client_id != self.client_id.as_str()
            || flow.redirect_uri != self.redirect.as_str()
            || flow.exp <= now
            || flow.iat > now + 60
            || flow.exp.checked_sub(flow.iat) != Some(FLOW_SECONDS)
            || !bool::from(
                flow.challenge
                    .as_bytes()
                    .ct_eq(challenge.as_str().as_bytes()),
            )
        {
            return Err(invalid());
        }
        let _permit = self
            .exchanges
            .try_acquire()
            .map_err(|_| Status::resource_exhausted("Google sign-in is busy; retry shortly"))?;
        let mut client = self.client(false).await?;
        let response = client
            .exchange_code(AuthorizationCode::new(request.code))
            .map_err(|_| Status::unavailable("Google token endpoint is unavailable"))?
            .set_pkce_verifier(verifier)
            .request_async(&self.http)
            .await
            .map_err(|error| match error {
                openidconnect::RequestTokenError::ServerResponse(ref response)
                    if *response.error()
                        == openidconnect::core::CoreErrorResponseType::InvalidGrant =>
                {
                    invalid()
                }
                openidconnect::RequestTokenError::Parse(..) => invalid(),
                _ => Status::unavailable("Google token exchange is unavailable"),
            })?;
        let id_token = response.id_token().ok_or_else(invalid)?;
        let nonce = Nonce::new(flow.sub);
        if matches!(
            id_token.signing_key(&client.id_token_verifier()),
            Err(SignatureVerificationError::NoMatchingKey)
        ) {
            client = self.client(true).await?;
        }
        let id_verifier = client
            .id_token_verifier()
            .set_allowed_algs([CoreJwsSigningAlgorithm::RsaSsaPkcs1V15Sha256]);
        let claims = id_token
            .claims(&id_verifier, &nonce)
            .map_err(|_| invalid())?;
        // This library leaves azp policy to callers. A Google login accepts only
        // this application, including when the token contains multiple audiences.
        if claims
            .authorized_party()
            .is_some_and(|party| party.as_str() != self.client_id.as_str())
            || (claims.audiences().len() > 1 && claims.authorized_party().is_none())
            || claims.issue_time() > Utc::now() + chrono::Duration::seconds(60)
            || claims.expiration() <= claims.issue_time()
            || claims
                .additional_claims()
                .nbf
                .is_some_and(|nbf| nbf > Utc::now().timestamp())
        {
            return Err(invalid());
        }
        if let Some(expected) = claims.access_token_hash() {
            let actual = AccessTokenHash::from_token(
                response.access_token(),
                id_token.signing_alg().map_err(|_| invalid())?,
                id_token.signing_key(&id_verifier).map_err(|_| invalid())?,
            )
            .map_err(|_| invalid())?;
            if actual != *expected {
                return Err(invalid());
            }
        }
        let email = claims.email().ok_or_else(invalid)?.as_str();
        let subject = claims.subject().as_str();
        if claims.email_verified() != Some(true)
            || !crate::email::valid_email(email)
            || subject.is_empty()
            || subject.len() > 255
        {
            return Err(invalid());
        }
        Ok(GoogleIdentity {
            subject: subject.into(),
            email: email.into(),
            name: claims
                .name()
                .and_then(|name| name.get(None))
                .map(|name| name.chars().take(255).collect()),
            picture: claims
                .picture()
                .and_then(|picture| picture.get(None))
                .and_then(|url| url::Url::parse(url.as_str()).ok())
                .filter(|url| url.scheme() == "https" && url.as_str().len() <= 8192)
                .map(String::from),
        })
    }
}

#[cfg(test)]
mod tests;
