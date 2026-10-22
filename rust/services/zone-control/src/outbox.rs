//! Leases over the existing product outboxes. External effects run after the
//! atomic claim commits; completion and deferral require the same live fence.

use serde_json::Value;
use sqlx::{types::Json, FromRow, PgPool, SqlitePool};
use std::num::NonZeroU32;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutboxKind {
    Runtime,
    GrantProjection,
}

impl OutboxKind {
    fn table(self) -> &'static str {
        match self {
            Self::Runtime => "zone_runtime_outbox",
            Self::GrantProjection => "zone_grant_projection_outbox",
        }
    }

    fn target_column(self) -> &'static str {
        match self {
            Self::Runtime => "operation_id",
            Self::GrantProjection => "grant_id",
        }
    }
}

/// Unique claim identity for one worker incarnation, independent of OS PIDs.
#[derive(Clone, Debug)]
pub struct WorkerId(String);

impl Default for WorkerId {
    fn default() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }
}

/// Facts to deliver; the fenced claim itself cannot be constructed by a caller.
pub struct OutboxLease {
    kind: OutboxKind,
    row: ClaimedRow,
    owner: WorkerId,
}

impl OutboxLease {
    pub fn kind(&self) -> OutboxKind {
        self.kind
    }

    pub fn target_id(&self) -> &str {
        &self.row.target_id
    }

    pub fn event_type(&self) -> &str {
        &self.row.event_type
    }

    pub fn payload(&self) -> &Value {
        &self.row.payload.0
    }
}

#[derive(FromRow)]
struct ClaimedRow {
    id: i64,
    target_id: String,
    event_type: String,
    payload: Json<Value>,
    payload_snapshot: String,
    generation: i32,
    fence: i64,
}

#[derive(Clone)]
enum Database {
    Sqlite(SqlitePool),
    Postgres(PgPool),
}

/// The composition root owns the pool and schema migrations. This repository
/// neither creates tables nor closes a borrowed pool.
#[derive(Clone)]
pub struct OutboxStore(Database);

#[derive(Debug, thiserror::Error)]
#[error("Zone outbox storage is unavailable")]
pub struct StoreUnavailable(#[from] sqlx::Error);

impl OutboxStore {
    pub async fn sqlite(pool: SqlitePool) -> Result<Self, StoreUnavailable> {
        let store = Self(Database::Sqlite(pool));
        store.validate_schema().await?;
        Ok(store)
    }

    pub async fn postgres(pool: PgPool) -> Result<Self, StoreUnavailable> {
        let store = Self(Database::Postgres(pool));
        store.validate_schema().await?;
        Ok(store)
    }

    async fn validate_schema(&self) -> Result<(), StoreUnavailable> {
        for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
            let sql = format!(
                "SELECT id, {}, event_type, payload, lease_owner, lease_expires_at, \
                 generation, fence, attempt_count, next_retry_at, processed_at \
                 FROM {} WHERE 1 = 0",
                kind.target_column(),
                kind.table()
            );
            match &self.0 {
                Database::Sqlite(pool) => {
                    sqlx::query(&sql).execute(pool).await?;
                }
                Database::Postgres(pool) => {
                    sqlx::query(&sql).execute(pool).await?;
                }
            };
        }
        Ok(())
    }

    /// Claim and return one due intent in a single committed SQL statement.
    /// PostgreSQL skips rows claimed by other transactions; SQLite serializes
    /// its UPDATE before evaluating the candidate subquery.
    pub async fn claim(
        &self,
        kind: OutboxKind,
        owner: &WorkerId,
        lease_seconds: NonZeroU32,
    ) -> Result<Option<OutboxLease>, StoreUnavailable> {
        let postgres = matches!(self.0, Database::Postgres(_));
        let due = if postgres {
            "processed_at IS NULL AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP) \
             AND (lease_owner IS NULL OR lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)"
        } else {
            "processed_at IS NULL AND (next_retry_at IS NULL OR julianday(next_retry_at) <= julianday('now')) \
             AND (lease_owner IS NULL OR lease_expires_at IS NULL OR julianday(lease_expires_at) <= julianday('now'))"
        };
        let expires = if postgres {
            "CURRENT_TIMESTAMP + ($2::bigint * INTERVAL '1 second')"
        } else {
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+' || $2 || ' seconds')"
        };
        let sql = format!(
            "UPDATE {table} SET lease_owner = $1, lease_expires_at = {expires}, \
             generation = generation + 1, fence = fence + 1 \
             WHERE id = (SELECT id FROM {table} WHERE {due} ORDER BY id LIMIT 1 {lock}) \
             RETURNING {id} AS id, {target} AS target_id, event_type, payload, \
             CAST(payload AS TEXT) AS payload_snapshot, generation, fence",
            table = kind.table(),
            target = kind.target_column(),
            lock = if postgres {
                "FOR UPDATE SKIP LOCKED"
            } else {
                ""
            },
            id = if postgres { "id::bigint" } else { "id" },
        );
        let seconds = i64::from(lease_seconds.get());
        // Drain RETURNING through statement completion. SQLite's single-row
        // fetch resets the statement before observing deferred commit errors.
        let rows = match &self.0 {
            Database::Sqlite(pool) => {
                sqlx::query_as::<_, ClaimedRow>(&sql)
                    .bind(&owner.0)
                    .bind(seconds)
                    .fetch_all(pool)
                    .await?
            }
            Database::Postgres(pool) => {
                sqlx::query_as::<_, ClaimedRow>(&sql)
                    .bind(&owner.0)
                    .bind(seconds)
                    .fetch_all(pool)
                    .await?
            }
        };
        Ok(rows.into_iter().next().map(|row| OutboxLease {
            kind,
            row,
            owner: owner.clone(),
        }))
    }

    /// Acknowledge a confirmed effect. False means the lease expired, completed
    /// already or belongs to a newer worker; the caller must abandon its result.
    pub async fn complete(&self, lease: &OutboxLease) -> Result<bool, StoreUnavailable> {
        self.finish(lease, None).await
    }

    /// Keep an unresolved effect pending with the SAME durable target and
    /// payload. A retry count never turns an unknown runtime outcome terminal.
    pub async fn defer(
        &self,
        lease: &OutboxLease,
        retry_seconds: NonZeroU32,
    ) -> Result<bool, StoreUnavailable> {
        self.finish(lease, Some(retry_seconds)).await
    }

    async fn finish(
        &self,
        lease: &OutboxLease,
        retry_seconds: Option<NonZeroU32>,
    ) -> Result<bool, StoreUnavailable> {
        let postgres = matches!(self.0, Database::Postgres(_));
        let assignments = match (postgres, retry_seconds) {
            (_, None) => "processed_at = CURRENT_TIMESTAMP, next_retry_at = NULL",
            (true, Some(_)) => {
                "next_retry_at = CURRENT_TIMESTAMP + ($8::bigint * INTERVAL '1 second')"
            }
            (false, Some(_)) => {
                "next_retry_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '+' || $8 || ' seconds')"
            }
        };
        let live = if postgres {
            "lease_expires_at > CURRENT_TIMESTAMP"
        } else {
            "julianday(lease_expires_at) > julianday('now')"
        };
        // Also bind the immutable intent: SQLite can recycle integer row IDs
        // after cleanup, so matching counters alone is insufficient.
        let sql = format!(
            "UPDATE {} SET {assignments}, attempt_count = attempt_count + 1, \
             lease_owner = NULL, lease_expires_at = NULL \
             WHERE id = CAST($1 AS BIGINT) AND lease_owner = $2 AND generation = $3 AND fence = $4 \
             AND {target} = $5 AND event_type = $6 AND CAST(payload AS TEXT) = $7 \
             AND processed_at IS NULL AND {live}",
            lease.kind.table(),
            target = lease.kind.target_column(),
        );
        let updated = match &self.0 {
            Database::Sqlite(pool) => {
                let query = sqlx::query(&sql)
                    .bind(lease.row.id)
                    .bind(&lease.owner.0)
                    .bind(lease.row.generation)
                    .bind(lease.row.fence)
                    .bind(&lease.row.target_id)
                    .bind(&lease.row.event_type)
                    .bind(&lease.row.payload_snapshot);
                let query = if let Some(delay) = retry_seconds {
                    query.bind(i64::from(delay.get()))
                } else {
                    query
                };
                query.execute(pool).await?.rows_affected()
            }
            Database::Postgres(pool) => {
                let query = sqlx::query(&sql)
                    .bind(lease.row.id)
                    .bind(&lease.owner.0)
                    .bind(lease.row.generation)
                    .bind(lease.row.fence)
                    .bind(&lease.row.target_id)
                    .bind(&lease.row.event_type)
                    .bind(&lease.row.payload_snapshot);
                let query = if let Some(delay) = retry_seconds {
                    query.bind(i64::from(delay.get()))
                } else {
                    query
                };
                query.execute(pool).await?.rows_affected()
            }
        };
        Ok(updated == 1)
    }
}
