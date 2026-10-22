use super::*;

#[tokio::test]
async fn deferred_commit_errors_never_return_an_executable_claim() {
    for postgres in databases() {
        let fixture = Fixture::new(postgres).await;
        fixture
            .execute(
                "CREATE TABLE permitted_claims (owner VARCHAR(128) PRIMARY KEY);
             CREATE TABLE claim_checks (owner VARCHAR(128) REFERENCES permitted_claims(owner)
               DEFERRABLE INITIALLY DEFERRED);",
            )
            .await;
        if postgres {
            fixture.execute(
                "CREATE FUNCTION verify_claim() RETURNS trigger LANGUAGE plpgsql AS $$
                 BEGIN INSERT INTO claim_checks (owner) VALUES (NEW.lease_owner); RETURN NEW; END $$;",
            ).await;
        }
        let store = fixture.store().await;
        for (index, kind) in [OutboxKind::Runtime, OutboxKind::GrantProjection]
            .into_iter()
            .enumerate()
        {
            fixture.seed(kind, 1).await;
            let (table, _) = names(kind);
            fixture.execute(&if postgres {
                format!("CREATE TRIGGER require_claim_{index} AFTER UPDATE OF lease_owner ON {table}
                    FOR EACH ROW WHEN (NEW.lease_owner IS NOT NULL) EXECUTE FUNCTION verify_claim();")
            } else {
                format!("CREATE TRIGGER require_claim_{index} AFTER UPDATE OF lease_owner ON {table}
                    WHEN NEW.lease_owner IS NOT NULL BEGIN
                    INSERT INTO claim_checks (owner) VALUES (NEW.lease_owner); END;")
            }).await;
            let result = store.claim(kind, &WorkerId::default(), seconds(30)).await;
            assert!(
                result.is_err(),
                "An uncommitted intent must never reach the effect handler"
            );
            assert_eq!(fixture.count(&format!("SELECT count(*) FROM {table} WHERE lease_owner IS NULL AND generation = 0 AND fence = 0 AND processed_at IS NULL")).await, 1);
            assert_eq!(fixture.count("SELECT count(*) FROM claim_checks").await, 0);
        }
        fixture.clean().await;
    }
}

#[tokio::test]
async fn recycled_ids_and_mutated_intents_reject_an_old_completion() {
    for postgres in databases() {
        let fixture = Fixture::new(postgres).await;
        let store = fixture.store().await;
        let owner = WorkerId::default();
        for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
            let (table, target) = names(kind);
            for mutation in [
                format!("{target} = 'another-target'"),
                "event_type = 'another-event'".to_owned(),
                "payload = '{\"zone_id\":\"another-zone\"}'".to_owned(),
            ] {
                fixture.execute(&format!("DELETE FROM {table}")).await;
                fixture.seed(kind, 1).await;
                let old = store
                    .claim(kind, &owner, seconds(30))
                    .await
                    .unwrap()
                    .unwrap();
                // Recreate a different intent with the same row ID, owner and
                // initial generation/fence. Only the durable intent differs.
                fixture.execute(&format!("DELETE FROM {table}")).await;
                fixture.seed(kind, 1).await;
                fixture
                    .execute(&format!("UPDATE {table} SET {mutation}"))
                    .await;
                let current = store
                    .claim(kind, &owner, seconds(30))
                    .await
                    .unwrap()
                    .unwrap();
                assert!(!store.complete(&old).await.unwrap());
                assert!(!store.defer(&old, seconds(1)).await.unwrap());
                assert!(store.complete(&current).await.unwrap());
            }
        }
        fixture.clean().await;
    }
}
