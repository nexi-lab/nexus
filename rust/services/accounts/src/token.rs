use chrono::Utc;
use jsonwebtoken::{decode, encode, Algorithm, DecodingKey, EncodingKey, Header, Validation};
use serde::{Deserialize, Serialize};
use tonic::Status;

use crate::store::User;

#[derive(Serialize, Deserialize)]
pub(crate) struct Claims {
    pub sub: String,
    pub subject_type: String,
    pub subject_id: String,
    pub email: String,
    pub iat: u64,
    pub exp: u64,
    #[serde(default)]
    pub purpose: Option<String>,
    #[serde(default)]
    pub zone_id: Option<String>,
    #[serde(default)]
    pub is_admin: bool,
    #[serde(default)]
    pub name: Option<String>,
}

pub(crate) struct AccountTokens {
    encoding: EncodingKey,
    decoding: DecodingKey,
    validation: Validation,
    ttl_seconds: u64,
}

impl AccountTokens {
    pub fn new(secret: &[u8], ttl_seconds: u64) -> Result<Self, Status> {
        if secret.len() < 32 || !(1..=86_400).contains(&ttl_seconds) {
            return Err(Status::invalid_argument(
                "JWT secret must be at least 32 bytes; token lifetime must be 1..86400 seconds",
            ));
        }
        let mut validation = Validation::new(Algorithm::HS256);
        validation.leeway = 0;
        validation.validate_nbf = true;
        validation.set_required_spec_claims(&["exp", "sub"]);
        Ok(Self {
            encoding: EncodingKey::from_secret(secret),
            decoding: DecodingKey::from_secret(secret),
            validation,
            ttl_seconds,
        })
    }

    pub fn issue(&self, user: &User) -> Result<String, Status> {
        let now = Utc::now().timestamp() as u64;
        let claims = Claims {
            sub: user.user_id.clone(),
            subject_type: "user".into(),
            subject_id: user.user_id.clone(),
            email: user
                .email
                .clone()
                .ok_or_else(|| Status::failed_precondition("Account has no email address"))?,
            iat: now,
            exp: now + self.ttl_seconds,
            purpose: Some("account".into()),
            zone_id: user.zone_id.clone(),
            is_admin: user.is_global_admin == 1,
            name: user.display_name.clone(),
        };
        encode(&Header::new(Algorithm::HS256), &claims, &self.encoding)
            .map_err(|_| Status::internal("Could not sign account token"))
    }

    pub fn verify(&self, authorization: &str) -> Result<Claims, Status> {
        let invalid = || Status::unauthenticated("Invalid or expired account token");
        let bearer = authorization
            .strip_prefix("Bearer ")
            .filter(|token| !token.is_empty())
            .ok_or_else(invalid)?;
        let claims = decode::<Claims>(bearer, &self.decoding, &self.validation)
            .map_err(|_| invalid())?
            .claims;
        let now = Utc::now().timestamp() as u64;
        if claims.subject_type != "user"
            || claims.sub != claims.subject_id
            || claims.sub.is_empty()
            || claims.email.is_empty()
            || claims.exp <= claims.iat
            || claims.exp <= now
            || claims.iat > now + 60
            || claims
                .purpose
                .as_deref()
                .is_some_and(|purpose| purpose != "account")
        {
            return Err(invalid());
        }
        Ok(claims)
    }
}
