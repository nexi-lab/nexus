use super::*;
use crate::{
    proto::{
        accounts_service_client::AccountsServiceClient,
        accounts_service_server::AccountsServiceServer,
    },
    store::AccountStore,
    AccountService,
};
use sqlx::{PgPool, SqlitePool};
use tokio_stream::wrappers::TcpListenerStream;

pub(super) const SECRET: &[u8] = b"fixture-google-flow-signing-secret-32-bytes";
pub(super) const REDIRECT: &str = "http://127.0.0.1:4173/oauth/callback";

pub(super) enum Pool {
    Sqlite(SqlitePool),
    Postgres(PgPool),
}

pub(super) enum WriteGate {
    Sqlite(sqlx::Transaction<'static, sqlx::Sqlite>),
    Postgres(sqlx::Transaction<'static, sqlx::Postgres>),
}

impl WriteGate {
    pub async fn release(self) {
        match self {
            Self::Sqlite(tx) => tx.commit().await,
            Self::Postgres(tx) => tx.commit().await,
        }
        .unwrap();
    }
}

macro_rules! pool_query {
    ($self:expr, $pool:ident, $query:expr) => {
        match &$self.pool {
            Pool::Sqlite($pool) => $query,
            Pool::Postgres($pool) => $query,
        }
    };
}

pub(super) struct DatabaseFixture {
    pub url: String,
    pool: Pool,
    temp: Option<tempfile::TempDir>,
    schema: Option<(PgPool, String)>,
}

pub(super) fn databases() -> Vec<bool> {
    let mut choices = vec![false];
    if std::env::var_os("ACCOUNTS_OAUTH_TEST_DATABASE_URL").is_some() {
        choices.push(true);
    }
    choices
}

impl DatabaseFixture {
    pub async fn new(postgres: bool) -> Self {
        let fixture = if postgres {
            let mut url =
                url::Url::parse(&std::env::var("ACCOUNTS_OAUTH_TEST_DATABASE_URL").unwrap())
                    .unwrap();
            assert!(matches!(url.scheme(), "postgres" | "postgresql"));
            assert!(matches!(
                url.host_str(),
                Some("127.0.0.1" | "localhost" | "[::1]")
            ));
            assert!(url
                .path()
                .trim_start_matches('/')
                .starts_with("nexus_accounts_test"));
            assert!(
                url.query().is_none(),
                "Test database URL must not override schema/options"
            );
            let admin = sqlx::postgres::PgPoolOptions::new()
                .max_connections(1)
                .connect(url.as_str())
                .await
                .unwrap();
            let schema = format!("oauth_fixture_{}", uuid::Uuid::new_v4().simple());
            sqlx::query(&format!("CREATE SCHEMA {schema}"))
                .execute(&admin)
                .await
                .unwrap();
            url.query_pairs_mut()
                .append_pair("options", &format!("-csearch_path={schema}"));
            url.query_pairs_mut()
                .append_pair("application_name", &schema);
            let pool = sqlx::postgres::PgPoolOptions::new()
                .max_connections(8)
                .connect(url.as_str())
                .await
                .unwrap();
            Self {
                url: url.into(),
                pool: Pool::Postgres(pool),
                temp: None,
                schema: Some((admin, schema)),
            }
        } else {
            let temp = tempfile::Builder::new()
                .prefix("nexus-google-test-")
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
            Self {
                url: format!("sqlite:///{}", path.display()),
                pool: Pool::Sqlite(pool),
                temp: Some(temp),
                schema: None,
            }
        };
        let schema = format!(
            "{}\n{}",
            include_str!("../../../tests/fixtures/users.sql"),
            include_str!("../../../tests/fixtures/oauth-accounts.sql")
        );
        fixture
            .execute(&if postgres {
                schema.replace("DATETIME", "TIMESTAMP")
            } else {
                schema
            })
            .await;
        eprintln!(
            "Google OAuth network fixture: {}",
            if postgres { "PostgreSQL" } else { "SQLite" }
        );
        fixture
    }

    pub async fn execute(&self, sql: &str) {
        pool_query!(
            self,
            pool,
            sqlx::raw_sql(sql).execute(pool).await.map(|_| ())
        )
        .unwrap();
    }

    pub async fn block_writes(&self) -> WriteGate {
        match &self.pool {
            Pool::Sqlite(pool) => {
                WriteGate::Sqlite(pool.begin_with("BEGIN IMMEDIATE").await.unwrap())
            }
            Pool::Postgres(pool) => {
                let mut tx = pool.begin().await.unwrap();
                sqlx::query("LOCK TABLE users IN SHARE MODE")
                    .execute(&mut *tx)
                    .await
                    .unwrap();
                WriteGate::Postgres(tx)
            }
        }
    }

    pub async fn wait_for_two_writers(&self, provider: &ProviderFixture, expected_requests: usize) {
        tokio::time::timeout(Duration::from_secs(10), async {
            loop {
                let ready = if let Pool::Postgres(pool) = &self.pool {
                    sqlx::query_scalar::<_, i64>("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND application_name = $1 AND state = 'active' AND wait_event_type = 'Lock' AND query LIKE 'INSERT INTO users%'")
                        .bind(&self.schema.as_ref().unwrap().1).fetch_one(pool).await.unwrap() == 2
                } else { provider.token_requests() >= expected_requests };
                if ready { break; }
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }).await.expect("Both independent account writers must reach the database gate");
    }

    pub async fn count(&self, sql: &str) -> i64 {
        pool_query!(
            self,
            pool,
            sqlx::query_scalar::<_, i64>(sql).fetch_one(pool).await
        )
        .unwrap()
    }

    pub async fn text(&self, sql: &str) -> Option<String> {
        pool_query!(
            self,
            pool,
            sqlx::query_scalar::<_, Option<String>>(sql)
                .fetch_one(pool)
                .await
        )
        .unwrap()
    }

    pub async fn clean(self) {
        pool_query!(self, pool, pool.close().await);
        if let Some((admin, schema)) = self.schema {
            assert!(
                schema.starts_with("oauth_fixture_")
                    && schema[14..].bytes().all(|c| c.is_ascii_hexdigit())
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

pub(super) async fn google(provider: &ProviderFixture) -> GoogleLogin {
    google_redirect(provider, REDIRECT).await
}

pub(super) async fn google_redirect(provider: &ProviderFixture, redirect: &str) -> GoogleLogin {
    GoogleLogin::configured(
        "fixture-client".into(),
        "fixture-client-secret".into(),
        redirect.into(),
        SECRET,
        IssuerUrl::new(provider.issuer.clone()).unwrap(),
        ProviderHttp(
            reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .timeout(Duration::from_secs(5))
                .build()
                .unwrap(),
        ),
    )
    .await
    .unwrap()
}

pub(super) struct Running {
    pub service: AccountService,
    pub http_url: String,
    pub grpc_url: String,
    store: AccountStore,
    stop: tokio::sync::watch::Sender<bool>,
    tasks: Vec<tokio::task::JoinHandle<()>>,
}

impl Running {
    pub async fn new(database: &DatabaseFixture, provider: &ProviderFixture) -> Self {
        Self::with_google(database, google(provider).await).await
    }

    pub async fn with_google(database: &DatabaseFixture, google: GoogleLogin) -> Self {
        let store = AccountStore::connect(&database.url).await.unwrap();
        let service = AccountService::new(store.clone(), SECRET, 3600, 2)
            .unwrap()
            .with_google_login(google)
            .await
            .unwrap();
        let http = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let grpc = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let http_url = format!("http://{}", http.local_addr().unwrap());
        let grpc_url = format!("http://{}", grpc.local_addr().unwrap());
        let (stop, _) = tokio::sync::watch::channel(false);
        let mut http_stop = stop.subscribe();
        let mut grpc_stop = stop.subscribe();
        let hservice = service.clone();
        let gservice = service.clone();
        let tasks = vec![
            tokio::spawn(async move {
                axum::serve(http, crate::http::router(hservice))
                    .with_graceful_shutdown(async move {
                        let _ = http_stop.changed().await;
                    })
                    .await
                    .unwrap();
            }),
            tokio::spawn(async move {
                tonic::transport::Server::builder()
                    .add_service(AccountsServiceServer::new(gservice))
                    .serve_with_incoming_shutdown(TcpListenerStream::new(grpc), async move {
                        let _ = grpc_stop.changed().await;
                    })
                    .await
                    .unwrap();
            }),
        ];
        Self {
            service,
            http_url,
            grpc_url,
            store,
            stop,
            tasks,
        }
    }

    pub async fn client(&self) -> AccountsServiceClient<tonic::transport::Channel> {
        AccountsServiceClient::connect(self.grpc_url.clone())
            .await
            .unwrap()
    }

    pub async fn begin(&self) -> (Value, String) {
        let response = reqwest::Client::new()
            .get(format!("{}/auth/oauth/google/authorize", self.http_url))
            .send()
            .await
            .unwrap();
        assert_eq!(response.status(), 200);
        assert_eq!(response.headers()["cache-control"], "no-store");
        let set_cookie = response.headers()["set-cookie"].to_str().unwrap();
        assert!(
            set_cookie.contains("HttpOnly")
                && set_cookie.contains("SameSite=Lax")
                && set_cookie.contains("Max-Age=600")
        );
        assert!(!set_cookie.contains("Domain="));
        let cookie = set_cookie.split(';').next().unwrap().to_owned();
        let start: Value = response.json().await.unwrap();
        assert_eq!(start.as_object().unwrap().len(), 2);
        assert!(start.get("browser_binding").is_none());
        (start, cookie)
    }

    pub async fn callback(&self, code: &str, state: &str, cookie: &str) -> (u16, Value) {
        let response = reqwest::Client::new()
            .post(format!("{}/auth/oauth/google/callback", self.http_url))
            .header("cookie", cookie)
            .json(&json!({"code":code,"state":state}))
            .send()
            .await
            .unwrap();
        assert_eq!(response.headers()["cache-control"], "no-store");
        assert!(response.headers()["set-cookie"]
            .to_str()
            .unwrap()
            .contains("Max-Age=0"));
        (response.status().as_u16(), response.json().await.unwrap())
    }

    pub async fn close(mut self) {
        self.stop.send(true).unwrap();
        for task in self.tasks.drain(..) {
            task.await.unwrap();
        }
        self.store.close().await;
    }
}

impl Drop for Running {
    fn drop(&mut self) {
        for task in &self.tasks {
            task.abort();
        }
    }
}
