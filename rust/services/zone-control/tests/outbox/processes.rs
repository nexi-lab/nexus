use super::*;
use std::{
    fs,
    path::Path,
    process::{Child, Command, Stdio},
    time::Instant,
};

struct OwnedChild(Child);

impl Drop for OwnedChild {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}

#[tokio::test]
async fn independent_process_claims_are_recoverable_after_the_workers_exit() {
    for postgres in databases() {
        let fixture = Fixture::new(postgres).await;
        let store = fixture.store().await;
        for kind in [OutboxKind::Runtime, OutboxKind::GrantProjection] {
            fixture.seed(kind, 1).await;
            fixture.seed(kind, 2).await;
            let barrier = tempfile::tempdir().unwrap();
            let mut children = Vec::new();
            for id in 0..2 {
                let mut command = Command::new(std::env::current_exe().unwrap());
                command
                    .args([
                        "--exact",
                        "processes::process_claim",
                        "--ignored",
                        "--nocapture",
                    ])
                    .env_clear()
                    .env("NEXUS_OUTBOX_CHILD_DIR", barrier.path())
                    .env("NEXUS_OUTBOX_CHILD_ID", id.to_string())
                    .env(
                        "NEXUS_OUTBOX_CHILD_KIND",
                        if kind == OutboxKind::Runtime {
                            "runtime"
                        } else {
                            "projection"
                        },
                    );
                for key in [
                    "SYSTEMROOT",
                    "WINDIR",
                    "PATH",
                    "PATHEXT",
                    "TEMP",
                    "TMP",
                    "COMSPEC",
                ] {
                    if let Some(value) = std::env::var_os(key) {
                        command.env(key, value);
                    }
                }
                if let Some(path) = &fixture.sqlite_path {
                    command.env("NEXUS_OUTBOX_CHILD_SQLITE", path);
                } else {
                    command.env(
                        "NEXUS_OUTBOX_CHILD_POSTGRES",
                        fixture.postgres_url.as_ref().unwrap(),
                    );
                }
                let output =
                    fs::File::create(barrier.path().join(format!("child-{id}.log"))).unwrap();
                command
                    .stdout(Stdio::from(output.try_clone().unwrap()))
                    .stderr(Stdio::from(output));
                children.push(OwnedChild(command.spawn().unwrap()));
            }
            tokio::time::timeout(Duration::from_secs(15), async {
                while !(barrier.path().join("ready-0").is_file()
                    && barrier.path().join("ready-1").is_file())
                {
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .expect("Both independent processes must reach the claim gate");
            fs::write(barrier.path().join("go"), b"go").unwrap();
            tokio::time::timeout(Duration::from_secs(15), async {
                loop {
                    let mut done = true;
                    for child in &mut children {
                        done &= child.0.try_wait().unwrap().is_some();
                    }
                    if done {
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            })
            .await
            .expect("Atomic claims must complete without a cross-process deadlock");
            for (id, child) in children.iter_mut().enumerate() {
                assert!(
                    child.0.wait().unwrap().success(),
                    "{}",
                    fs::read_to_string(barrier.path().join(format!("child-{id}.log"))).unwrap()
                );
            }
            let results: BTreeSet<_> = (0..2)
                .map(|id| fs::read_to_string(barrier.path().join(format!("claim-{id}"))).unwrap())
                .collect();
            assert_eq!(
                results,
                BTreeSet::from(["target-1".to_owned(), "target-2".to_owned()])
            );
            let (table, _) = names(kind);
            assert_eq!(fixture.count(&format!("SELECT count(DISTINCT lease_owner) FROM {table} WHERE generation = 1 AND fence = 1 AND processed_at IS NULL AND attempt_count = 0")).await, 2);
            // The exited workers leave durable claims, not false completion.
            // Expire those leases and recover the exact same intent targets.
            fixture
                .execute(&format!(
                    "UPDATE {table} SET lease_expires_at = '2000-01-01T00:00:00Z'"
                ))
                .await;
            let owner = WorkerId::default();
            for expected in ["target-1", "target-2"] {
                let lease = store
                    .claim(kind, &owner, seconds(30))
                    .await
                    .unwrap()
                    .unwrap();
                assert_eq!(lease.target_id(), expected);
                assert!(store.complete(&lease).await.unwrap());
            }
        }
        fixture.clean().await;
    }
}

#[tokio::test]
#[ignore = "Private subprocess entry exercised by the parent process test"]
async fn process_claim() {
    let folder = std::env::var("NEXUS_OUTBOX_CHILD_DIR").unwrap();
    let folder = Path::new(&folder);
    let id = std::env::var("NEXUS_OUTBOX_CHILD_ID").unwrap();
    fs::write(folder.join(format!("ready-{id}")), b"ready").unwrap();
    let deadline = Instant::now() + Duration::from_secs(15);
    while !folder.join("go").exists() {
        assert!(
            Instant::now() < deadline,
            "Parent did not release the claim gate"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    let pool = if let Ok(path) = std::env::var("NEXUS_OUTBOX_CHILD_SQLITE") {
        Pool::Sqlite(
            SqlitePoolOptions::new()
                .max_connections(1)
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
            sqlx::postgres::PgPoolOptions::new()
                .max_connections(1)
                .connect(&std::env::var("NEXUS_OUTBOX_CHILD_POSTGRES").unwrap())
                .await
                .unwrap(),
        )
    };
    let store = pool.store().await;
    let kind = match std::env::var("NEXUS_OUTBOX_CHILD_KIND").unwrap().as_str() {
        "runtime" => OutboxKind::Runtime,
        "projection" => OutboxKind::GrantProjection,
        _ => panic!("Unknown fixture outbox kind"),
    };
    let lease = store
        .claim(kind, &WorkerId::default(), seconds(30))
        .await
        .unwrap()
        .unwrap();
    fs::write(folder.join(format!("claim-{id}")), lease.target_id()).unwrap();
    pool.close().await;
}
