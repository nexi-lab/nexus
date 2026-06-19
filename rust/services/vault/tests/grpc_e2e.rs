//! gRPC end-to-end integration test for the dogfood signing path.
//!
//! Locks in the empirical findings of the 2026-06-13 verification:
//!
//!   Discovery (1) — pre-Phase-P, external `PutSecret` returned
//!                   `UNIMPLEMENTED` because cluster routes never
//!                   wired the plugin service. Phase P fixes this at
//!                   the cluster layer; here we keep a regression
//!                   test of vault's tonic-server contract so any
//!                   future tonic-trait drift on vault's side fails
//!                   at PR time rather than dogfood-runtime.
//!
//!   Discovery (2) — vault's data layout (`NEXUS_DATA_DIR/vault/`
//!                   with `master.key` + `vault-meta.redb` + `content/`)
//!                   is created by `create_vault` and must round-trip
//!                   plaintext through real network transport.
//!
//! The test stands up `GenericSecretsServiceImpl` against a fresh
//! kernel + path-local backend in a temp dir, hosts it under a real
//! `tonic::transport::Server` bound to `127.0.0.1:0`, and exercises:
//!
//!   1. `PutSecret` → `GetSecret` round-trip                  (plaintext path)
//!   2. `GetSecretSealed` → `PutSecretSealed` (fresh key)
//!      → `GetSecret`                                          (sealed path)
//!
//! Non-localhost peer rejection is covered by the unit tests in
//! `services::generic_secrets::tests::sealed_handlers_reject_non_loopback_peer`
//! — the integration test can only observe real TCP peers and so
//! always sees loopback by construction.

use std::sync::Arc;

use kernel::abc::object_store::ObjectStore;
use services::generic_secrets::proto::generic_secrets_service_client::GenericSecretsServiceClient;
use services::generic_secrets::proto::generic_secrets_service_server::GenericSecretsServiceServer;
use services::generic_secrets::proto::*;
use services::generic_secrets::GenericSecretsServiceImpl;
use services::password_vault::crypto;
use tempfile::TempDir;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;

struct Harness {
    _dir: TempDir,
    addr: std::net::SocketAddr,
    shutdown: tokio::sync::oneshot::Sender<()>,
    server: tokio::task::JoinHandle<()>,
}

impl Harness {
    async fn start() -> Self {
        let dir = TempDir::new().unwrap();
        let vault_dir = dir.path().join("vault");
        std::fs::create_dir_all(&vault_dir).unwrap();

        let kernel = Arc::new(kernel::kernel::Kernel::new());
        let meta_path = vault_dir.join("vault-meta.redb");
        kernel
            .set_metastore_path(meta_path.to_str().unwrap())
            .unwrap();

        let content_dir = vault_dir.join("content");
        let backend: Arc<dyn ObjectStore> = Arc::new(
            backends::storage::path_local::PathLocalBackend::new(&content_dir, true).unwrap(),
        );
        let backend_name = backend.name().to_string();

        kernel
            .sys_setattr(
                "/vault",
                /* DT_MOUNT */ 2,
                &backend_name,
                Some(backend),
                None,
                None,
                "memory",
                "root",
                false,
                0,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
            .unwrap();

        let master_key_path = vault_dir.join("master.key");
        let master_key = crypto::load_or_create_master_key(&master_key_path).unwrap();

        let svc =
            GenericSecretsServiceImpl::new_on_existing_mount(kernel, "/vault", master_key).unwrap();

        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();

        let (shutdown_tx, shutdown_rx) = tokio::sync::oneshot::channel::<()>();
        let server = tokio::spawn(async move {
            tonic::transport::Server::builder()
                .add_service(GenericSecretsServiceServer::new(svc))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async move {
                    let _ = shutdown_rx.await;
                })
                .await
                .expect("tonic server");
        });

        Self {
            _dir: dir,
            addr,
            shutdown: shutdown_tx,
            server,
        }
    }

    fn url(&self) -> String {
        format!("http://{}", self.addr)
    }

    async fn shutdown(self) {
        let _ = self.shutdown.send(());
        let _ = self.server.await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn put_get_round_trip_through_real_grpc_transport() {
    let h = Harness::start().await;
    let mut client = GenericSecretsServiceClient::connect(h.url()).await.unwrap();

    let put = client
        .put_secret(PutSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            value: "sk-grpc-e2e".into(),
            description: Some("E2E".into()),
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(put.metadata.as_ref().unwrap().current_version, 1);

    let got = client
        .get_secret(GetSecretRequest {
            namespace: "provider:openai".into(),
            key: "api_key".into(),
            version: None,
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(got.value, "sk-grpc-e2e");
    assert_eq!(got.version, 1);

    h.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn sealed_round_trip_through_real_grpc_transport() {
    let h = Harness::start().await;
    let mut client = GenericSecretsServiceClient::connect(h.url()).await.unwrap();

    client
        .put_secret(PutSecretRequest {
            namespace: "signing-keys".into(),
            key: "kernel-dogfood-v1".into(),
            value: "ed25519-grpc-e2e-bytes".into(),
            description: None,
        })
        .await
        .unwrap();

    let sealed = client
        .get_secret_sealed(GetSecretRequest {
            namespace: "signing-keys".into(),
            key: "kernel-dogfood-v1".into(),
            version: None,
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(sealed.nonce.len(), 12);
    assert!(!sealed.ciphertext.is_empty());

    client
        .put_secret_sealed(PutSecretSealedRequest {
            namespace: "signing-keys".into(),
            key: "kernel-dogfood-v1-restored".into(),
            nonce: sealed.nonce,
            ciphertext: sealed.ciphertext,
            description: Some("rehydrated by E2E".into()),
        })
        .await
        .unwrap();

    let restored = client
        .get_secret(GetSecretRequest {
            namespace: "signing-keys".into(),
            key: "kernel-dogfood-v1-restored".into(),
            version: None,
        })
        .await
        .unwrap()
        .into_inner();
    assert_eq!(restored.value, "ed25519-grpc-e2e-bytes");

    h.shutdown().await;
}
