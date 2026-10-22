//! Password and Google creation share one database transaction and collision policy.

use super::*;
use crate::oauth::GoogleIdentity;
use std::time::Duration;
use tokio::time::{sleep, Instant};

struct Registration<'a> {
    email: &'a str,
    username: Option<&'a str>,
    name: &'a str,
    picture: Option<&'a str>,
    authentication: Authentication<'a>,
}

#[derive(Clone, Copy)]
enum Authentication<'a> {
    Password(&'a str),
    Google(&'a GoogleIdentity),
}

impl<'a> Authentication<'a> {
    fn google(self) -> Option<&'a GoogleIdentity> {
        match self {
            Self::Google(identity) => Some(identity),
            Self::Password(_) => None,
        }
    }
}

impl AccountStore {
    pub(crate) async fn require_google_schema(&self) -> Result<(), Status> {
        with_pool!(self, pool, sqlx::query("SELECT oauth_account_id, user_id, provider, provider_user_id, provider_email, created_at, last_used_at FROM user_oauth_accounts WHERE 1 = 0").fetch_all(pool).await.map(|_| ()))
            .map_err(unavailable)
    }

    pub(crate) async fn create_password_user(
        &self,
        email: &str,
        username: Option<&str>,
        display_name: &str,
        password_hash: &str,
    ) -> Result<User, Status> {
        self.register_account(Registration {
            email,
            username,
            name: display_name,
            picture: None,
            authentication: Authentication::Password(password_hash),
        })
        .await
    }

    pub(crate) async fn login_google(&self, identity: &GoogleIdentity) -> Result<User, Status> {
        self.register_account(Registration {
            email: &identity.email,
            username: None,
            name: identity
                .name
                .as_deref()
                .filter(|name| !name.is_empty())
                .unwrap_or_else(|| identity.email.split('@').next().unwrap_or_default()),
            picture: identity.picture.as_deref(),
            authentication: Authentication::Google(identity),
        })
        .await
    }

    async fn register_account(&self, account: Registration<'_>) -> Result<User, Status> {
        let user_id = uuid::Uuid::new_v4().to_string();
        let binding_id = uuid::Uuid::new_v4().to_string();
        let now = Utc::now().naive_utc();
        let begin = match self.0 {
            Database::Sqlite(_) => "BEGIN IMMEDIATE",
            Database::Postgres(_) => "BEGIN ISOLATION LEVEL SERIALIZABLE",
        };
        let lookup = format!(
            "SELECT EXISTS(SELECT 1 FROM users WHERE (email = $1 OR username = $2) AND {ACTIVE})"
        );
        let insert = format!(
            "INSERT INTO users (user_id, email, username, display_name, avatar_url, password_hash, \
            primary_auth_method, is_global_admin, is_active, email_verified, created_at, updated_at, last_login_at) \
            VALUES ($1, $2, $3, $4, $5, $6, $7, 0, 1, $8, $9, $9, $10) RETURNING {COLUMNS}"
        );
        let login = format!(
            "UPDATE users SET last_login_at = $1, updated_at = $1 WHERE user_id = $2 \
            AND email IS NOT NULL AND email_verified = 1 AND {ACTIVE} RETURNING {COLUMNS}"
        );
        // Provider calls and password hashing complete before this transaction.
        // PostgreSQL retries the whole transaction on SSI/identity races. SQLite
        // acquires its writer before checking either email or provider subject.
        let mut retry_deadline = None;
        let mut retry_delay_ms = 10;
        loop {
            let result: Result<Result<User, Status>, sqlx::Error> = with_pool!(
                self,
                pool,
                async {
                    let mut transaction = pool.begin_with(begin).await?;
                    if let Some(identity) = account.authentication.google() {
                        let owner: Option<String> = sqlx::query_scalar(
                            "SELECT user_id FROM user_oauth_accounts WHERE provider = 'google' AND provider_user_id = $1",
                        )
                        .bind(&identity.subject)
                        .fetch_optional(&mut *transaction)
                        .await?;
                        if let Some(owner) = owner {
                            // A durable subject binding wins even when the
                            // provider email changed. Never recreate a disabled
                            // identity or copy provider profile over user edits.
                            let user = sqlx::query_as::<_, User>(&login)
                                .bind(now)
                                .bind(&owner)
                                .fetch_optional(&mut *transaction)
                                .await?;
                            if let Some(user) = user {
                                sqlx::query("UPDATE user_oauth_accounts SET last_used_at = $1, provider_email = $2 WHERE provider = 'google' AND provider_user_id = $3")
                                    .bind(now).bind(&identity.email).bind(&identity.subject)
                                    .execute(&mut *transaction).await?;
                                transaction.commit().await?;
                                return Ok(Ok(user));
                            }
                            transaction.rollback().await?;
                            return Ok(Err(Status::unauthenticated("Linked account is unavailable or unverified")));
                        }
                    }
                    let exists: bool = sqlx::query_scalar(&lookup)
                        .bind(account.email)
                        .bind(account.username)
                        .fetch_one(&mut *transaction)
                        .await?;
                    if exists {
                        transaction.rollback().await?;
                        return Ok(Err(Status::already_exists(
                            "Email or username is already registered; sign in with the existing method",
                        )));
                    }
                    let google = account.authentication.google().is_some();
                    let hash = match account.authentication { Authentication::Password(hash) => Some(hash), Authentication::Google(_) => None };
                    let user = sqlx::query_as::<_, User>(&insert)
                        .bind(&user_id)
                        .bind(account.email)
                        .bind(account.username)
                        .bind(account.name)
                        .bind(account.picture)
                        .bind(hash)
                        .bind(if google { "oauth" } else { "password" })
                        .bind(i32::from(google))
                        .bind(now)
                        .bind(if google { Some(now) } else { None })
                        .fetch_one(&mut *transaction)
                        .await?;
                    if let Some(identity) = account.authentication.google() {
                        // Identity and binding commit together. No provider
                        // tokens, state, verifier or pending signup are stored.
                        sqlx::query("INSERT INTO user_oauth_accounts (oauth_account_id, user_id, provider, provider_user_id, provider_email, created_at, last_used_at) VALUES ($1, $2, 'google', $3, $4, $5, $5)")
                            .bind(&binding_id).bind(&user_id).bind(&identity.subject)
                            .bind(&identity.email).bind(now)
                            .execute(&mut *transaction).await?;
                    }
                    transaction.commit().await?;
                    Ok(Ok(user))
                }
                .await
            );
            match result {
                Ok(result) => return result,
                Err(error)
                    if error.as_database_error().is_some_and(|error| {
                        error.code().as_deref() == Some("40001") || error.is_unique_violation()
                    }) =>
                {
                    // SSI conflicts may repeat while the winning transaction
                    // is still committing. Yield with bounded jitter instead
                    // of consuming a fixed retry count in a tight loop.
                    // The failed transaction has rolled back before waiting;
                    // neither a connection nor a row lock is held here.
                    let deadline = *retry_deadline
                        .get_or_insert_with(|| Instant::now() + Duration::from_secs(2));
                    let jitter = u64::from(uuid::Uuid::new_v4().as_bytes()[0]);
                    let delay = Duration::from_millis(
                        retry_delay_ms / 2 + jitter % (retry_delay_ms / 2 + 1),
                    );
                    if Instant::now() + delay >= deadline {
                        return Err(Status::aborted("Concurrent account change; retry sign-in"));
                    }
                    sleep(delay).await;
                    retry_delay_ms = (retry_delay_ms * 2).min(100);
                }
                Err(error) => return Err(unavailable(error)),
            }
        }
    }
}
