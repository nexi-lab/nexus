//! Unix socket IPC server for Python-Rust communication.
//!
//! The daemon listens on a Unix socket and accepts JSON-RPC commands from Python.
//! This enables Python to orchestrate Rust FUSE operations for 10-100x performance.

use crate::client::NexusClient;
use crate::error::NexusClientError;
use base64::Engine;
use log::{debug, error, info, warn};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::time::Instant;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::signal;

/// JSON-RPC request from Python client.
#[derive(Debug, Deserialize)]
struct JsonRpcRequest {
    jsonrpc: String,
    id: Option<Value>,
    method: String,
    params: Value,
}

/// JSON-RPC response to Python client.
#[derive(Debug, Serialize)]
struct JsonRpcResponse {
    jsonrpc: String,
    id: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<JsonRpcError>,
}

#[derive(Debug, Serialize)]
struct JsonRpcError {
    code: i32,
    message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    data: Option<Value>,
}

impl JsonRpcResponse {
    fn success(id: Option<Value>, result: Value) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id,
            result: Some(result),
            error: None,
        }
    }

    fn error(id: Option<Value>, code: i32, message: String, errno: Option<i32>) -> Self {
        Self {
            jsonrpc: "2.0".to_string(),
            id,
            result: None,
            error: Some(JsonRpcError {
                code,
                message,
                data: errno.map(|e| json!({"errno": e})),
            }),
        }
    }
}

/// Daemon configuration.
pub struct DaemonConfig {
    pub socket_path: PathBuf,
    pub nexus_url: String,
    pub api_key: String,
    pub agent_id: Option<String>,
}

/// Unix socket IPC daemon.
pub struct Daemon {
    config: DaemonConfig,
    client: NexusClient,
}

impl Daemon {
    /// Create a new daemon instance.
    pub fn new(config: DaemonConfig) -> Result<Self, NexusClientError> {
        let client = NexusClient::new(&config.nexus_url, &config.api_key, config.agent_id.clone())?;

        Ok(Self { config, client })
    }

    /// Start the daemon and listen for connections.
    pub async fn run(self) -> anyhow::Result<()> {
        // Remove existing socket if it exists
        if self.config.socket_path.exists() {
            std::fs::remove_file(&self.config.socket_path)?;
        }

        // Create Unix socket listener
        let listener = UnixListener::bind(&self.config.socket_path)?;

        // Restrict socket permissions to owner-only (Issue 18A).
        // Prevents other users on the same host from connecting to the daemon
        // and issuing API calls with the owner's credentials.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(
                &self.config.socket_path,
                std::fs::Permissions::from_mode(0o700),
            )?;
        }

        info!(
            "Rust FUSE daemon listening on {}",
            self.config.socket_path.display()
        );

        // Print socket path to stdout for Python to read
        println!("{}", self.config.socket_path.display());

        // Setup graceful shutdown on SIGTERM/SIGINT
        let shutdown = signal::ctrl_c();
        tokio::pin!(shutdown);

        loop {
            tokio::select! {
                Ok((stream, _)) = listener.accept() => {
                    debug!("New connection accepted");
                    let client = self.client.clone();
                    tokio::spawn(async move {
                        if let Err(e) = handle_connection(stream, client).await {
                            error!("Connection error: {}", e);
                        }
                    });
                }
                _ = &mut shutdown => {
                    info!("Received shutdown signal, cleaning up...");
                    break;
                }
            }
        }

        // Cleanup socket
        if self.config.socket_path.exists() {
            std::fs::remove_file(&self.config.socket_path)?;
        }

        info!("Daemon shutdown complete");
        Ok(())
    }
}

/// Handle a single Unix socket connection.
async fn handle_connection(stream: UnixStream, client: NexusClient) -> anyhow::Result<()> {
    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut line = String::new();

    loop {
        line.clear();
        let n = reader.read_line(&mut line).await?;

        if n == 0 {
            debug!("Connection closed");
            break;
        }

        let response = match serde_json::from_str::<JsonRpcRequest>(&line) {
            Ok(request) => handle_request(request, &client).await,
            Err(e) => {
                error!("Failed to parse JSON-RPC request: {}", e);
                JsonRpcResponse::error(None, -32700, format!("Parse error: {}", e), None)
            }
        };

        let mut response_json = serde_json::to_string(&response)?;
        response_json.push('\n');
        writer.write_all(response_json.as_bytes()).await?;
        writer.flush().await?;
    }

    Ok(())
}

/// Issue 5A: Generic param extraction to eliminate 8x identical boilerplate.
/// Deserializes JSON params into a typed struct, returning a consistent error
/// on failure.
fn extract_params<T: for<'de> Deserialize<'de>>(params: &Value) -> Result<T, NexusClientError> {
    serde_json::from_value(params.clone())
        .map_err(|e| NexusClientError::InvalidResponse(format!("Invalid params: {}", e)))
}

/// Handle a single JSON-RPC request.
async fn handle_request(request: JsonRpcRequest, client: &NexusClient) -> JsonRpcResponse {
    debug!("Handling method: {}", request.method);

    // Clone client for spawn_blocking
    let client = client.clone();
    let method = request.method.clone();
    let params = request.params.clone();

    // Run blocking operations in a separate thread pool
    let result = tokio::task::spawn_blocking(move || match method.as_str() {
        "read" => handle_read(&params, &client),
        "write" => handle_write(&params, &client),
        "list" => handle_list(&params, &client),
        "stat" => handle_stat(&params, &client),
        "mkdir" => handle_mkdir(&params, &client),
        "delete" => handle_delete(&params, &client),
        "rename" => handle_rename(&params, &client),
        "exists" => handle_exists(&params, &client),
        _ => Err(NexusClientError::InvalidResponse(format!(
            "Method not found: {}",
            method
        ))),
    })
    .await;

    let result = match result {
        Ok(r) => r,
        Err(e) => {
            error!("Task join error: {}", e);
            return JsonRpcResponse::error(
                request.id,
                -32603,
                format!("Internal error: {}", e),
                None,
            );
        }
    };

    match result {
        Ok(value) => JsonRpcResponse::success(request.id, value),
        Err(e) => {
            let errno = e.to_errno();
            warn!("Request failed: {} (errno={})", e, errno);
            JsonRpcResponse::error(request.id, -32603, e.to_string(), Some(errno))
        }
    }
}

// Handler functions — Issue 5A: use extract_params<T>() to eliminate
// repeated deserialization boilerplate.

fn handle_read(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    let started_at = Instant::now();
    let content = match client.read(&p.path) {
        Ok(content) => {
            crate::metrics::record_read("backend", content.len(), started_at.elapsed());
            content
        }
        Err(error) => {
            crate::metrics::record_read("error", 0, started_at.elapsed());
            return Err(error);
        }
    };
    let encoded = base64::engine::general_purpose::STANDARD.encode(&content);

    Ok(json!({
        "__type__": "bytes",
        "data": encoded
    }))
}

fn handle_write(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct ContentBytes {
        #[serde(rename = "__type__")]
        type_tag: String,
        data: String,
    }
    #[derive(Deserialize)]
    struct P {
        path: String,
        content: ContentBytes,
    }
    let p: P = extract_params(params)?;

    let content = base64::engine::general_purpose::STANDARD
        .decode(&p.content.data)
        .map_err(|e| NexusClientError::InvalidResponse(format!("Invalid base64: {}", e)))?;

    client.write(&p.path, &content)?;
    crate::metrics::record_write_backend_rpc();
    Ok(json!({}))
}

fn handle_list(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    let files = client.list(&p.path)?;
    Ok(json!({ "files": files }))
}

fn handle_stat(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    let metadata = client.stat(&p.path)?;
    Ok(serde_json::to_value(metadata)
        .map_err(|e| NexusClientError::InvalidResponse(format!("Serialization error: {}", e)))?)
}

fn handle_mkdir(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    client.mkdir(&p.path)?;
    Ok(json!({}))
}

fn handle_delete(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    client.delete(&p.path)?;
    Ok(json!({}))
}

fn handle_rename(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        old_path: String,
        new_path: String,
    }
    let p: P = extract_params(params)?;

    client.rename(&p.old_path, &p.new_path)?;
    Ok(json!({}))
}

fn handle_exists(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    let exists = client.exists(&p.path);
    Ok(json!({ "exists": exists }))
}

#[cfg(test)]
mod tests {
    use super::*;

    use base64::Engine;
    use mockito::Server;

    fn test_guard() -> std::sync::MutexGuard<'static, ()> {
        crate::metrics::test_guard()
    }

    #[test]
    fn daemon_read_records_backend_metrics_on_success() {
        let _guard = test_guard();
        crate::metrics::reset_for_tests();
        let mut server = Server::new();
        let payload = base64::engine::general_purpose::STANDARD.encode(b"daemon");

        let _mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(format!(
                r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{payload}"}}}}"#
            ))
            .create();

        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();

        let result = handle_read(&json!({"path": "/daemon.txt"}), &client).unwrap();

        assert_eq!(result["data"], payload);
        let metrics = crate::metrics::render();
        assert!(metrics.contains("nexus_read_bytes_total{tier=\"backend\"} 6"));
        assert!(metrics.contains("nexus_read_latency_seconds_count{tier=\"backend\"} 1"));
    }

    #[test]
    fn daemon_read_records_error_metrics_on_failure() {
        let _guard = test_guard();
        crate::metrics::reset_for_tests();
        let mut server = Server::new();

        let _mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(500)
            .with_body("server error")
            .create();

        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();

        let result = handle_read(&json!({"path": "/daemon.txt"}), &client);

        assert!(result.is_err());
        let metrics = crate::metrics::render();
        assert!(metrics.contains("nexus_read_bytes_total{tier=\"error\"} 0"));
        assert!(metrics.contains("nexus_read_latency_seconds_count{tier=\"error\"} 1"));
    }

    #[test]
    fn daemon_write_records_backend_rpc_on_success() {
        let _guard = test_guard();
        crate::metrics::reset_for_tests();
        let mut server = Server::new();
        let payload = base64::engine::general_purpose::STANDARD.encode(b"daemon");

        let _mock = server
            .mock("POST", "/api/nfs/write")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{}}"#)
            .create();

        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();

        handle_write(
            &json!({
                "path": "/daemon.txt",
                "content": {"__type__": "bytes", "data": payload}
            }),
            &client,
        )
        .unwrap();

        assert!(crate::metrics::render().contains("nexus_write_backend_rpc_total 1"));
    }
}
