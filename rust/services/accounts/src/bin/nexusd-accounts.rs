use clap::Parser;
use nexus_accounts::{
    proto::accounts_service_server::AccountsServiceServer, store::AccountStore, AccountService,
};
use std::net::SocketAddr;
use tokio::net::TcpListener;
use tokio_stream::wrappers::TcpListenerStream;

#[derive(Parser)]
struct Args {
    #[arg(
        long,
        env = "NEXUS_ACCOUNTS_GRPC_ADDR",
        default_value = "127.0.0.1:12015"
    )]
    grpc_addr: SocketAddr,
    #[arg(
        long,
        env = "NEXUS_ACCOUNTS_HTTP_ADDR",
        default_value = "127.0.0.1:12016"
    )]
    http_addr: SocketAddr,
    #[arg(long, default_value_t = 3600)]
    token_ttl_seconds: u64,
    #[arg(long, default_value_t = 2)]
    password_workers: usize,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .init();
    let args = Args::parse();
    if !args.grpc_addr.ip().is_loopback() || !args.http_addr.ip().is_loopback() {
        return Err(
            "Account listeners require loopback; terminate external TLS at the gateway".into(),
        );
    }
    let url = std::env::var("NEXUS_DATABASE_URL").map_err(|_| "NEXUS_DATABASE_URL is required")?;
    let secret = std::env::var("NEXUS_JWT_SECRET").map_err(|_| "NEXUS_JWT_SECRET is required")?;
    let store = AccountStore::connect(&url).await?;
    let service = AccountService::new(
        store.clone(),
        secret.as_bytes(),
        args.token_ttl_seconds,
        args.password_workers,
    )?;
    let grpc = TcpListener::bind(args.grpc_addr).await?;
    let http = TcpListener::bind(args.http_addr).await?;
    println!(
        "{}",
        serde_json::json!({"grpc_addr": grpc.local_addr()?.to_string(), "http_addr": http.local_addr()?.to_string()})
    );
    let (shutdown, _) = tokio::sync::watch::channel(false);
    let mut grpc_shutdown = shutdown.subscribe();
    let mut http_shutdown = shutdown.subscribe();
    let grpc_server = tonic::transport::Server::builder()
        .add_service(
            AccountsServiceServer::new(service.clone()).max_decoding_message_size(32 * 1024),
        )
        .serve_with_incoming_shutdown(TcpListenerStream::new(grpc), async move {
            let _ = grpc_shutdown.changed().await;
        });
    let http_server = axum::serve(http, nexus_accounts::http::router(service))
        .with_graceful_shutdown(async move {
            let _ = http_shutdown.changed().await;
        });
    let servers = async {
        tokio::try_join!(
            async { grpc_server.await.map_err(std::io::Error::other) },
            async { http_server.await }
        )
    };
    tokio::pin!(servers);
    tokio::select! {
        result = &mut servers => { result?; }
        result = tokio::signal::ctrl_c() => {
            result?;
            shutdown.send(true)?;
            servers.await?;
        }
    }
    store.close().await;
    Ok(())
}
