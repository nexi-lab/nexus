//! Queries against the existing `users` table. Schema changes belong to migrations.

use chrono::{NaiveDateTime, Utc};
use sqlx::{postgres::PgPoolOptions, sqlite::SqlitePoolOptions, FromRow, PgPool, SqlitePool};
use std::str::FromStr;
use tonic::Status;

use crate::proto::UserProfile;

const COLUMNS: &str = "user_id, email, username, display_name, avatar_url, password_hash, \
    primary_auth_method, is_global_admin, email_verified, zone_id, created_at, last_login_at";
const ACTIVE: &str = "is_active = 1 AND deleted_at IS NULL";

#[derive(Clone)]
enum Database {
    Sqlite(SqlitePool),
    Postgres(PgPool),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sqlite_locations_follow_sqlalchemy_rules_on_every_platform() {
        for (url, filename) in [
            ("sqlite:///relative.db", "relative.db"),
            ("sqlite:////tmp/accounts.db", "/tmp/accounts.db"),
            ("sqlite:///C:/accounts.db", "C:/accounts.db"),
            (
                "sqlite+pysqlite:////tmp/nexus%20accounts.db",
                "/tmp/nexus accounts.db",
            ),
        ] {
            assert_eq!(
                sqlite_options(url).unwrap().get_filename(),
                std::path::Path::new(filename)
            );
        }
        assert_eq!(
            sqlite_options("sqlite://ambiguous.db").unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
    }

    #[tokio::test]
    async fn concurrent_password_updates_cannot_overwrite_a_committed_change() {
        let temp = tempfile::Builder::new()
            .prefix("nexus-account-cas-")
            .tempdir()
            .unwrap();
        let path = temp.path().join("accounts.db");
        let pool = SqlitePool::connect_with(
            sqlx::sqlite::SqliteConnectOptions::new()
                .filename(&path)
                .create_if_missing(true),
        )
        .await
        .unwrap();
        sqlx::raw_sql(include_str!("../tests/fixtures/users.sql"))
            .execute(&pool)
            .await
            .unwrap();
        sqlx::query("INSERT INTO users (user_id, password_hash, primary_auth_method, is_global_admin, is_active, email_verified, created_at, updated_at) VALUES ('alice', 'original-hash', 'password', 0, 1, 1, $1, $1)")
            .bind(Utc::now().naive_utc()).execute(&pool).await.unwrap();
        let store = AccountStore::connect(&format!("sqlite:///{}", path.display()))
            .await
            .unwrap();
        let snapshot = store.by_id("alice").await.unwrap();
        let (first, second) = tokio::join!(
            store.change_password(&snapshot, "first-hash"),
            store.change_password(&snapshot, "second-hash")
        );
        assert_eq!(first.is_ok() as usize + second.is_ok() as usize, 1);
        let expected = if first.is_ok() {
            "first-hash"
        } else {
            "second-hash"
        };
        let error = first.err().or_else(|| second.err()).unwrap();
        assert_eq!(error.code(), tonic::Code::Aborted);
        assert_eq!(
            store.by_id("alice").await.unwrap().password_hash.as_deref(),
            Some(expected)
        );
        store.close().await;
        pool.close().await;
        temp.close().unwrap();
    }
}

#[derive(Clone)]
pub struct AccountStore(Database);

#[derive(FromRow)]
pub(crate) struct User {
    pub user_id: String,
    pub email: Option<String>,
    pub username: Option<String>,
    pub display_name: Option<String>,
    pub avatar_url: Option<String>,
    pub password_hash: Option<String>,
    pub primary_auth_method: String,
    pub is_global_admin: i32,
    pub email_verified: i32,
    pub zone_id: Option<String>,
    pub created_at: NaiveDateTime,
    pub last_login_at: Option<NaiveDateTime>,
}

impl User {
    pub fn profile(&self) -> UserProfile {
        UserProfile {
            user_id: self.user_id.clone(),
            email: self.email.clone(),
            username: self.username.clone(),
            display_name: self.display_name.clone(),
            avatar_url: self.avatar_url.clone(),
            primary_auth_method: self.primary_auth_method.clone(),
            is_global_admin: self.is_global_admin == 1,
            email_verified: self.email_verified == 1,
            zone_id: self.zone_id.clone(),
            created_at: self.created_at.and_utc().to_rfc3339(),
            last_login_at: self.last_login_at.map(|time| time.and_utc().to_rfc3339()),
        }
    }
}

// Both drivers execute the same SQL; only the pool type differs.
macro_rules! with_pool {
    ($store:expr, $pool:ident, $query:expr) => {
        match &$store.0 {
            Database::Sqlite($pool) => $query,
            Database::Postgres($pool) => $query,
        }
    };
}

fn unavailable(error: sqlx::Error) -> Status {
    tracing::error!(%error, "account database operation failed");
    Status::unavailable("Account database is unavailable")
}

fn sqlite_options(url: &str) -> Result<sqlx::sqlite::SqliteConnectOptions, Status> {
    let url = url.replacen("sqlite+pysqlite:", "sqlite:", 1);
    let path = url.strip_prefix("sqlite:///").ok_or_else(|| {
        Status::invalid_argument("SQLite database URLs require the SQLAlchemy sqlite:/// format")
    })?;
    sqlx::sqlite::SqliteConnectOptions::from_str(&format!("sqlite://{path}"))
        .map(|options| options.create_if_missing(false))
        .map_err(|_| Status::invalid_argument("Invalid SQLite database URL"))
}

impl AccountStore {
    pub async fn close(&self) {
        with_pool!(self, pool, pool.close().await);
    }

    /// Connect to a provisioned account database; never create an empty fallback.
    pub async fn connect(url: &str) -> Result<Self, Status> {
        let store = if url.starts_with("sqlite:") || url.starts_with("sqlite+pysqlite:") {
            Self(Database::Sqlite(
                SqlitePoolOptions::new()
                    .max_connections(4)
                    .connect_with(sqlite_options(url)?)
                    .await
                    .map_err(unavailable)?,
            ))
        } else if url.starts_with("postgres:") || url.starts_with("postgresql") {
            let url = url
                .replacen("postgresql+psycopg2:", "postgresql:", 1)
                .replacen("postgresql+psycopg:", "postgresql:", 1);
            Self(Database::Postgres(
                PgPoolOptions::new()
                    .max_connections(8)
                    .connect(&url)
                    .await
                    .map_err(unavailable)?,
            ))
        } else {
            return Err(Status::invalid_argument(
                "Expected a SQLite or PostgreSQL database URL",
            ));
        };
        let sql = format!("SELECT {COLUMNS} FROM users WHERE 1 = 0");
        with_pool!(
            store,
            pool,
            sqlx::query_as::<_, User>(&sql).fetch_all(pool).await
        )
        .map_err(unavailable)?;
        Ok(store)
    }

    pub(crate) async fn by_id(&self, id: &str) -> Result<User, Status> {
        let sql = format!("SELECT {COLUMNS} FROM users WHERE user_id = $1 AND {ACTIVE}");
        with_pool!(
            self,
            pool,
            sqlx::query_as::<_, User>(&sql)
                .bind(id)
                .fetch_optional(pool)
                .await
        )
        .map_err(unavailable)?
        .ok_or_else(|| Status::unauthenticated("Account is unavailable"))
    }

    pub(crate) async fn for_login(&self, identifier: &str) -> Result<User, Status> {
        for column in ["email", "username"] {
            let sql =
                format!("SELECT {COLUMNS} FROM users WHERE {column} = $1 AND {ACTIVE} LIMIT 2");
            let mut users = with_pool!(
                self,
                pool,
                sqlx::query_as::<_, User>(&sql)
                    .bind(identifier)
                    .fetch_all(pool)
                    .await
            )
            .map_err(unavailable)?;
            match users.len() {
                0 => continue,
                1 => return Ok(users.remove(0)),
                _ => {
                    return Err(Status::unauthenticated(
                        "Invalid email/username or password",
                    ))
                }
            }
        }
        Err(Status::unauthenticated(
            "Invalid email/username or password",
        ))
    }

    pub(crate) async fn record_login(&self, user: &User) -> Result<User, Status> {
        let sql = format!(
            "UPDATE users SET last_login_at = $1, updated_at = $1 WHERE user_id = $2 \
            AND password_hash = $3 AND email_verified = 1 AND {ACTIVE} RETURNING {COLUMNS}"
        );
        with_pool!(
            self,
            pool,
            sqlx::query_as::<_, User>(&sql)
                .bind(Utc::now().naive_utc())
                .bind(&user.user_id)
                .bind(&user.password_hash)
                .fetch_optional(pool)
                .await
        )
        .map_err(unavailable)?
        .ok_or_else(|| Status::unauthenticated("Account changed during sign-in"))
    }

    pub(crate) async fn update_profile(
        &self,
        user: &User,
        name: Option<&str>,
        avatar: Option<&str>,
    ) -> Result<User, Status> {
        let sql = format!(
            "UPDATE users SET display_name = COALESCE($1, display_name), \
            avatar_url = COALESCE($2, avatar_url), updated_at = $3 WHERE user_id = $4 \
            AND {ACTIVE} RETURNING {COLUMNS}"
        );
        with_pool!(
            self,
            pool,
            sqlx::query_as::<_, User>(&sql)
                .bind(name)
                .bind(avatar)
                .bind(Utc::now().naive_utc())
                .bind(&user.user_id)
                .fetch_optional(pool)
                .await
        )
        .map_err(unavailable)?
        .ok_or_else(|| Status::unauthenticated("Account is unavailable"))
    }

    pub(crate) async fn change_password(
        &self,
        user: &User,
        password_hash: &str,
    ) -> Result<(), Status> {
        let sql = format!(
            "UPDATE users SET password_hash = $1, updated_at = $2 \
            WHERE user_id = $3 AND password_hash = $4 AND {ACTIVE} RETURNING user_id"
        );
        let updated = with_pool!(
            self,
            pool,
            sqlx::query_scalar::<_, String>(&sql)
                .bind(password_hash)
                .bind(Utc::now().naive_utc())
                .bind(&user.user_id)
                .bind(&user.password_hash)
                .fetch_optional(pool)
                .await
        )
        .map_err(unavailable)?;
        updated.ok_or_else(|| Status::aborted("Account changed during password update"))?;
        Ok(())
    }
}
