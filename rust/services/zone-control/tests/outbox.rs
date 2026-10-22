use nexus_zone_control::outbox::{OutboxKind, OutboxStore, WorkerId};
use sqlx::sqlite::SqlitePoolOptions;
use std::{collections::BTreeSet, num::NonZeroU32, time::Duration};

#[path = "outbox/failures.rs"]
mod failures;
#[path = "outbox/processes.rs"]
mod processes;
#[path = "outbox/support.rs"]
mod support;
use support::{databases, names, Fixture, Pool};

fn seconds(value: u32) -> NonZeroU32 {
    NonZeroU32::new(value).unwrap()
}

#[tokio::test]
async fn missing_schema_is_rejected_without_creating_product_state() {
    let pool = SqlitePoolOptions::new()
        .max_connections(1)
        .connect("sqlite::memory:")
        .await
        .unwrap();
    let error = OutboxStore::sqlite(pool.clone()).await.err().unwrap();
    assert_eq!(error.to_string(), "Zone outbox storage is unavailable");
    assert_eq!(
        sqlx::query_scalar::<_, i64>("SELECT count(*) FROM sqlite_master WHERE type = 'table'")
            .fetch_one(&pool)
            .await
            .unwrap(),
        0
    );
    pool.close().await;
}

#[tokio::test]
async fn independent_pools_claim_distinct_intents_and_complete_once() {
    for postgres in databases() {
        let fixture = Fixture::new(postgres).await;
        let first = fixture.store().await;
        let second_pool = fixture.second_pool().await;
        let second = second_pool.store().await;
        for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
            fixture.seed(kind, 1).await;
            fixture.seed(kind, 2).await;
            let (owner_a, owner_b) = (WorkerId::default(), WorkerId::default());
            let (a, b) = tokio::join!(
                first.claim(kind, &owner_a, seconds(30)),
                second.claim(kind, &owner_b, seconds(30)),
            );
            let (a, b) = (a.unwrap().unwrap(), b.unwrap().unwrap());
            assert_eq!(
                BTreeSet::from([a.target_id(), b.target_id()]),
                BTreeSet::from(["target-1", "target-2"])
            );
            assert_eq!(a.kind(), kind);
            assert_eq!(a.event_type(), "effect");
            assert!(first
                .claim(kind, &WorkerId::default(), seconds(30))
                .await
                .unwrap()
                .is_none());
            assert!(first.complete(&a).await.unwrap());
            assert!(!first.complete(&a).await.unwrap());
            assert!(second.complete(&b).await.unwrap());
            let (table, _) = names(kind);
            assert_eq!(fixture.count(&format!("SELECT count(*) FROM {table} WHERE processed_at IS NOT NULL AND attempt_count = 1 AND lease_owner IS NULL AND lease_expires_at IS NULL")).await, 2);
        }
        second_pool.close().await;
        fixture.clean().await;
    }
}

#[tokio::test]
async fn expired_and_superseded_workers_cannot_complete_or_defer_a_new_claim() {
    for postgres in databases() {
        for same_owner in [false, true] {
            let fixture = Fixture::new(postgres).await;
            let store = fixture.store().await;
            let first_owner = WorkerId::default();
            let next_owner = if same_owner {
                first_owner.clone()
            } else {
                WorkerId::default()
            };
            for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
                fixture.seed(kind, 1).await;
                let first = store
                    .claim(kind, &first_owner, seconds(30))
                    .await
                    .unwrap()
                    .unwrap();
                let (table, _) = names(kind);
                fixture
                    .execute(&format!(
                        "UPDATE {table} SET lease_expires_at = '2000-01-01T00:00:00Z'"
                    ))
                    .await;
                assert!(!store.complete(&first).await.unwrap());
                assert!(!store.defer(&first, seconds(1)).await.unwrap());
                let next = store
                    .claim(kind, &next_owner, seconds(30))
                    .await
                    .unwrap()
                    .unwrap();
                assert_eq!(next.target_id(), first.target_id());
                assert_eq!(next.payload(), first.payload());
                assert!(!store.complete(&first).await.unwrap());
                assert!(!store.defer(&first, seconds(1)).await.unwrap());
                assert_eq!(fixture.count(&format!("SELECT count(*) FROM {table} WHERE generation = 2 AND fence = 2 AND attempt_count = 0 AND processed_at IS NULL")).await, 1);
                assert!(store.complete(&next).await.unwrap());
            }
            fixture.clean().await;
        }
    }
}

#[tokio::test]
async fn unresolved_effects_keep_the_same_target_and_payload_after_any_retry_count() {
    for postgres in databases() {
        let fixture = Fixture::new(postgres).await;
        let store = fixture.store().await;
        for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
            fixture.seed(kind, 1).await;
            let (table, _) = names(kind);
            fixture
                .execute(&format!("UPDATE {table} SET attempt_count = 1000"))
                .await;
            let lease = store
                .claim(kind, &WorkerId::default(), seconds(30))
                .await
                .unwrap()
                .unwrap();
            assert!(store.defer(&lease, seconds(3600)).await.unwrap());
            assert!(!store.complete(&lease).await.unwrap());
            assert!(store
                .claim(kind, &WorkerId::default(), seconds(30))
                .await
                .unwrap()
                .is_none());
            assert_eq!(fixture.count(&format!("SELECT count(*) FROM {table} WHERE processed_at IS NULL AND attempt_count = 1001 AND lease_owner IS NULL")).await, 1);
            fixture
                .execute(&format!(
                    "UPDATE {table} SET next_retry_at = '2000-01-01T00:00:00Z'"
                ))
                .await;
            let next = store
                .claim(kind, &WorkerId::default(), seconds(30))
                .await
                .unwrap()
                .unwrap();
            assert_eq!(next.payload(), lease.payload());
            assert_eq!(next.target_id(), lease.target_id());
            assert!(store.complete(&next).await.unwrap());
        }
        fixture.clean().await;
    }
}

#[tokio::test]
async fn a_revoked_fence_cannot_write_even_when_owner_and_generation_still_match() {
    for postgres in databases() {
        let fixture = Fixture::new(postgres).await;
        let store = fixture.store().await;
        for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
            fixture.seed(kind, 1).await;
            let lease = store
                .claim(kind, &WorkerId::default(), seconds(30))
                .await
                .unwrap()
                .unwrap();
            let (table, _) = names(kind);
            fixture
                .execute(&format!("UPDATE {table} SET fence = fence + 1"))
                .await;
            assert!(!store.complete(&lease).await.unwrap());
            assert!(!store.defer(&lease, seconds(1)).await.unwrap());
            assert_eq!(fixture.count(&format!("SELECT count(*) FROM {table} WHERE processed_at IS NULL AND attempt_count = 0")).await, 1);
        }
        fixture.clean().await;
    }
}

#[tokio::test]
async fn postgres_claim_skips_an_older_row_locked_by_another_transaction() {
    if !databases().contains(&true) {
        return;
    }
    let fixture = Fixture::new(true).await;
    fixture.seed(OutboxKind::Runtime, 1).await;
    fixture.seed(OutboxKind::Runtime, 2).await;
    let store = fixture.store().await;
    let Pool::Postgres(pool) = &fixture.pool else {
        unreachable!()
    };
    let mut gate = pool.begin().await.unwrap();
    sqlx::query("SELECT id FROM zone_runtime_outbox WHERE id = 1 FOR UPDATE")
        .execute(&mut *gate)
        .await
        .unwrap();
    let lease = tokio::time::timeout(
        Duration::from_secs(2),
        store.claim(OutboxKind::Runtime, &WorkerId::default(), seconds(30)),
    )
    .await
    .expect("A locked row must not block an independent due intent")
    .unwrap()
    .unwrap();
    assert_eq!(lease.target_id(), "target-2");
    gate.rollback().await.unwrap();
    assert!(store.complete(&lease).await.unwrap());
    fixture.clean().await;
}
