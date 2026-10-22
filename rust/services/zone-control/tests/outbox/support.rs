use nexus_zone_control::outbox::{OutboxKind, OutboxStore};
use sqlx::{postgres::PgPoolOptions, sqlite::SqlitePoolOptions, PgPool, SqlitePool};
use std::{path::PathBuf, time::Duration};

#[derive(Clone)]
pub enum Pool {
    Sqlite(SqlitePool),
    Postgres(PgPool),
}

pub struct Fixture {
    pub pool: Pool,
    pub sqlite_path: Option<PathBuf>,
    pub postgres_url: Option<String>,
    temp: Option<tempfile::TempDir>,
    schema: Option<(PgPool, String)>,
}

pub fn databases() -> Vec<bool> {
    let mut choices = vec![false];
    if std::env::var_os("NEXUS_ZONE_CONTROL_TEST_DATABASE_URL").is_some() {
        choices.push(true);
    }
    choices
}

impl Fixture {
    pub async fn new(postgres: bool) -> Self {
        let fixture = if postgres {
            let mut url = reqwest::Url::parse(
                &std::env::var("NEXUS_ZONE_CONTROL_TEST_DATABASE_URL").unwrap(),
            )
            .unwrap();
            assert!(matches!(url.scheme(), "postgres" | "postgresql"));
            assert!(matches!(
                url.host_str(),
                Some("127.0.0.1" | "localhost" | "[::1]")
            ));
            assert!(url
                .path()
                .trim_start_matches('/')
                .starts_with("nexus_zone_test"));
            assert!(
                url.query().is_none(),
                "Fixture URL cannot override schema/options"
            );
            let admin = PgPoolOptions::new()
                .max_connections(1)
                .connect(url.as_str())
                .await
                .unwrap();
            let schema = format!("outbox_fixture_{}", uuid::Uuid::new_v4().simple());
            sqlx::query(&format!("CREATE SCHEMA {schema}"))
                .execute(&admin)
                .await
                .unwrap();
            url.query_pairs_mut()
                .append_pair("options", &format!("-csearch_path={schema}"));
            let pool = PgPoolOptions::new()
                .max_connections(3)
                .connect(url.as_str())
                .await
                .unwrap();
            Self {
                pool: Pool::Postgres(pool),
                postgres_url: Some(url.to_string()),
                sqlite_path: None,
                temp: None,
                schema: Some((admin, schema)),
            }
        } else {
            let temp = tempfile::tempdir().unwrap();
            let path = temp.path().join("outbox.db");
            let pool = SqlitePoolOptions::new()
                .max_connections(3)
                .connect_with(
                    sqlx::sqlite::SqliteConnectOptions::new()
                        .filename(&path)
                        .create_if_missing(true)
                        .foreign_keys(true)
                        .busy_timeout(Duration::from_secs(5)),
                )
                .await
                .unwrap();
            Self {
                pool: Pool::Sqlite(pool),
                sqlite_path: Some(path),
                postgres_url: None,
                temp: Some(temp),
                schema: None,
            }
        };
        fixture
            .execute(if postgres {
                include_str!("../fixtures/zone-outboxes.postgres.sql")
            } else {
                include_str!("../fixtures/zone-outboxes.sqlite.sql")
            })
            .await;
        fixture
    }

    pub async fn store(&self) -> OutboxStore {
        self.pool.store().await
    }

    pub async fn second_pool(&self) -> Pool {
        if let Some(path) = &self.sqlite_path {
            Pool::Sqlite(
                SqlitePoolOptions::new()
                    .max_connections(2)
                    .connect_with(
                        sqlx::sqlite::SqliteConnectOptions::new()
                            .filename(path)
                            .foreign_keys(true)
                            .busy_timeout(Duration::from_secs(5)),
                    )
                    .await
                    .unwrap(),
            )
        } else {
            Pool::Postgres(
                PgPoolOptions::new()
                    .max_connections(2)
                    .connect(self.postgres_url.as_ref().unwrap())
                    .await
                    .unwrap(),
            )
        }
    }

    pub async fn execute(&self, sql: &str) {
        match &self.pool {
            Pool::Sqlite(pool) => {
                sqlx::raw_sql(sql).execute(pool).await.unwrap();
            }
            Pool::Postgres(pool) => {
                sqlx::raw_sql(sql).execute(pool).await.unwrap();
            }
        }
    }

    pub async fn count(&self, sql: &str) -> i64 {
        match &self.pool {
            Pool::Sqlite(pool) => sqlx::query_scalar(sql).fetch_one(pool).await.unwrap(),
            Pool::Postgres(pool) => sqlx::query_scalar(sql).fetch_one(pool).await.unwrap(),
        }
    }

    pub async fn seed(&self, kind: OutboxKind, id: i32) {
        let (table, target) = names(kind);
        self.execute(&format!(
            "INSERT INTO {table} (id, {target}, event_type, payload, generation, fence, attempt_count, created_at) \
             VALUES ({id}, 'target-{id}', 'effect', '{{\"zone_id\":\"z-test\",\"marker\":\"preserved-context\"}}', 0, 0, 0, CURRENT_TIMESTAMP)"
        )).await;
    }

    pub async fn clean(self) {
        self.pool.close().await;
        if let Some((admin, schema)) = self.schema {
            assert!(
                schema.starts_with("outbox_fixture_")
                    && schema[15..].bytes().all(|ch| ch.is_ascii_hexdigit())
            );
            sqlx::query(&format!("DROP SCHEMA {schema} CASCADE"))
                .execute(&admin)
                .await
                .unwrap();
            admin.close().await;
        }
        if let Some(temp) = self.temp {
            temp.close().unwrap();
        }
    }
}

impl Pool {
    pub async fn store(&self) -> OutboxStore {
        match self {
            Self::Sqlite(pool) => OutboxStore::sqlite(pool.clone()).await.unwrap(),
            Self::Postgres(pool) => OutboxStore::postgres(pool.clone()).await.unwrap(),
        }
    }

    pub async fn close(&self) {
        match self {
            Self::Sqlite(pool) => pool.close().await,
            Self::Postgres(pool) => pool.close().await,
        }
    }
}

pub fn names(kind: OutboxKind) -> (&'static str, &'static str) {
    match kind {
        OutboxKind::Runtime => ("zone_runtime_outbox", "operation_id"),
        OutboxKind::GrantProjection => ("zone_grant_projection_outbox", "grant_id"),
    }
}
