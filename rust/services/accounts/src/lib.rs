//! Human accounts use the existing SQL identities, bcrypt hashes and account JWTs.
//! File credentials and relationship grants remain the native daemon's authority.

pub mod http;
mod password;
pub mod store;
mod token;

use std::sync::Arc;
use tonic::{Request, Response, Status};

pub mod proto {
    tonic::include_proto!("nexus.accounts.v1");
}

use password::Passwords;
use proto::{accounts_service_server::AccountsService, *};
use store::{AccountStore, User};
use token::AccountTokens;

#[derive(Clone)]
pub struct AccountService {
    store: AccountStore,
    tokens: Arc<AccountTokens>,
    passwords: Passwords,
}

impl AccountService {
    pub fn new(
        store: AccountStore,
        jwt_secret: &[u8],
        token_ttl: u64,
        password_workers: usize,
    ) -> Result<Self, Status> {
        Ok(Self {
            store,
            tokens: Arc::new(AccountTokens::new(jwt_secret, token_ttl)?),
            passwords: Passwords::new(password_workers)?,
        })
    }

    async fn caller<T>(&self, request: &Request<T>) -> Result<User, Status> {
        let authorization = request
            .metadata()
            .get("authorization")
            .and_then(|value| value.to_str().ok())
            .ok_or_else(|| Status::unauthenticated("Account token is required"))?;
        let claims = self.tokens.verify(authorization)?;
        let user = self.store.by_id(&claims.subject_id).await?;
        if user.email.as_deref() != Some(&claims.email) {
            return Err(Status::unauthenticated("Account identity changed"));
        }
        Ok(user)
    }
}

#[tonic::async_trait]
impl AccountsService for AccountService {
    async fn login(
        &self,
        request: Request<LoginRequest>,
    ) -> Result<Response<LoginResponse>, Status> {
        let body = request.into_inner();
        if body.identifier.is_empty()
            || body.identifier.chars().count() > 255
            || body.password.is_empty()
        {
            return Err(Status::invalid_argument(
                "Identifier and password are required",
            ));
        }
        let user = self.store.for_login(&body.identifier).await?;
        let stored = user
            .password_hash
            .clone()
            .ok_or_else(|| Status::unauthenticated("Invalid email/username or password"))?;
        self.passwords.verify(body.password, stored).await?;
        if user.email_verified != 1 {
            return Err(Status::permission_denied("Email is not verified"));
        }
        let user = self.store.record_login(&user).await?;
        Ok(Response::new(LoginResponse {
            token: self.tokens.issue(&user)?,
            user: Some(user.profile()),
        }))
    }

    async fn get_profile(
        &self,
        request: Request<GetProfileRequest>,
    ) -> Result<Response<UserProfile>, Status> {
        Ok(Response::new(self.caller(&request).await?.profile()))
    }

    async fn update_profile(
        &self,
        request: Request<UpdateProfileRequest>,
    ) -> Result<Response<UserProfile>, Status> {
        let user = self.caller(&request).await?;
        let body = request.into_inner();
        if body
            .display_name
            .as_ref()
            .is_some_and(|name| name.chars().count() > 255)
            || body.avatar_url.as_ref().is_some_and(|url| url.len() > 8192)
        {
            return Err(Status::invalid_argument("Profile field is too long"));
        }
        let user = self
            .store
            .update_profile(
                &user,
                body.display_name.as_deref(),
                body.avatar_url.as_deref(),
            )
            .await?;
        Ok(Response::new(user.profile()))
    }

    async fn change_password(
        &self,
        request: Request<ChangePasswordRequest>,
    ) -> Result<Response<ChangePasswordResponse>, Status> {
        let user = self.caller(&request).await?;
        let body = request.into_inner();
        if body.new_password.chars().count() < 8 {
            return Err(Status::invalid_argument(
                "New password must be at least eight characters",
            ));
        }
        let stored = user
            .password_hash
            .clone()
            .ok_or_else(|| Status::failed_precondition("Account has no password"))?;
        self.passwords.verify(body.current_password, stored).await?;
        let hash = self.passwords.hash(body.new_password).await?;
        self.store.change_password(&user, &hash).await?;
        Ok(Response::new(ChangePasswordResponse { success: true }))
    }
}
