//! Unix socket IPC server for Python-Rust communication.
//!
//! The daemon listens on a Unix socket and accepts JSON-RPC commands from Python.
//! This enables Python to orchestrate Rust FUSE operations for 10-100x performance.

use crate::cache::FileCache;
use crate::cached_read::read_with_cache;
use crate::client::{InitializeResponse, NexusClient};
use crate::error::NexusClientError;
use base64::Engine;
use log::{debug, error, info, warn};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::signal;

/// JSON-RPC request from Python client.
#[derive(Debug, Deserialize)]
struct JsonRpcRequest {
    #[serde(rename = "jsonrpc")]
    _jsonrpc: String,
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
    pub file_cache: Option<Arc<FileCache>>,
}

/// Unix socket IPC daemon.
pub struct Daemon {
    config: DaemonConfig,
    client: NexusClient,
    file_cache: Option<Arc<FileCache>>,
    capabilities: Option<InitializeResponse>,
}

impl Daemon {
    /// Create a new daemon instance.
    pub fn new(config: DaemonConfig) -> Result<Self, NexusClientError> {
        let client = NexusClient::new(&config.nexus_url, &config.api_key, config.agent_id.clone())?;
        let capabilities = client.capabilities()?;

        Ok(Self {
            file_cache: config.file_cache.clone(),
            config,
            client,
            capabilities,
        })
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
        let shutdown = shutdown_signal();
        tokio::pin!(shutdown);

        loop {
            tokio::select! {
                Ok((stream, _)) = listener.accept() => {
                    debug!("New connection accepted");
                    let client = self.client.clone();
                    let file_cache = self.file_cache.clone();
                    let capabilities = self.capabilities.clone();
                    tokio::spawn(async move {
                        if let Err(e) = handle_connection(stream, client, file_cache, capabilities).await {
                            error!("Connection error: {}", e);
                        }
                    });
                }
                shutdown_result = &mut shutdown => {
                    if let Err(e) = shutdown_result {
                        warn!("Shutdown signal handler failed: {}", e);
                    }
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

async fn shutdown_signal() -> anyhow::Result<()> {
    #[cfg(unix)]
    {
        let mut sigterm = signal::unix::signal(signal::unix::SignalKind::terminate())?;
        tokio::select! {
            result = signal::ctrl_c() => {
                result?;
            }
            _ = sigterm.recv() => {}
        }
    }

    #[cfg(not(unix))]
    {
        signal::ctrl_c().await?;
    }

    Ok(())
}

/// Handle a single Unix socket connection.
async fn handle_connection(
    stream: UnixStream,
    client: NexusClient,
    file_cache: Option<Arc<FileCache>>,
    capabilities: Option<InitializeResponse>,
) -> anyhow::Result<()> {
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
            Ok(request) => {
                handle_request(request, &client, file_cache.clone(), capabilities.clone()).await
            }
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
async fn handle_request(
    request: JsonRpcRequest,
    client: &NexusClient,
    file_cache: Option<Arc<FileCache>>,
    capabilities: Option<InitializeResponse>,
) -> JsonRpcResponse {
    debug!("Handling method: {}", request.method);

    let client_owned = client.clone();
    let method = request.method.clone();
    let params = request.params.clone();
    let capabilities = capabilities.clone();

    // Async-first dispatch: cache_warm is async, everything else is sync via spawn_blocking.
    let result = if method == "cache_warm" {
        handle_cache_warm(params, Arc::new(client_owned), file_cache).await
    } else {
        let cache_for_blocking = file_cache.clone();
        let join = tokio::task::spawn_blocking(move || match method.as_str() {
            "read" => handle_read(&params, &client_owned, cache_for_blocking.as_deref()),
            "write" => handle_write(
                &params,
                &client_owned,
                cache_for_blocking.as_deref(),
                capabilities.as_ref(),
            ),
            "list" => handle_list(&params, &client_owned),
            "stat" => handle_stat(&params, &client_owned),
            "mkdir" => handle_mkdir(&params, &client_owned, capabilities.as_ref()),
            "delete" => handle_delete(
                &params,
                &client_owned,
                cache_for_blocking.as_deref(),
                capabilities.as_ref(),
            ),
            "rename" => handle_rename(
                &params,
                &client_owned,
                cache_for_blocking.as_deref(),
                capabilities.as_ref(),
            ),
            "exists" => handle_exists(&params, &client_owned),
            _ => Err(NexusClientError::InvalidResponse(format!(
                "Method not found: {}",
                method
            ))),
        })
        .await;
        match join {
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

fn ensure_capability(
    capabilities: Option<&InitializeResponse>,
    path: &str,
    capability: &str,
) -> Result<(), NexusClientError> {
    let Some(response) = capabilities else {
        return Ok(());
    };
    match response.capabilities.capability_for_path(path, capability) {
        Some(false) => Err(NexusClientError::UnsupportedCapability {
            capability: capability.to_string(),
            path: path.to_string(),
        }),
        _ => Ok(()),
    }
}

fn handle_read(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    let started_at = Instant::now();
    // Issue #4055 R4: when cache is enabled, fail closed on stat errors
    // rather than substituting gen=0. Any cached entry stored at gen=0
    // (including entries warmed by cache_warm before the backend bumped
    // generation, or entries cached when stat returned no gen) would
    // otherwise be served back to a caller whose stat just failed with
    // 403/404/etc. — leaking content past current authorization. With
    // no cache the gen value is irrelevant, so we keep the simpler path.
    let gen = if file_cache.is_some() {
        match client.stat(&p.path) {
            Ok(meta) => meta.gen,
            Err(err) => {
                crate::metrics::record_read("error", 0, started_at.elapsed());
                return Err(err);
            }
        }
    } else {
        0
    };
    let read_result = match read_with_cache(client, file_cache, &p.path, gen) {
        Ok(result) => {
            crate::metrics::record_read(result.tier, result.content.len(), started_at.elapsed());
            result
        }
        Err(error) => {
            crate::metrics::record_read("error", 0, started_at.elapsed());
            return Err(error);
        }
    };
    let encoded = base64::engine::general_purpose::STANDARD.encode(&read_result.content);

    Ok(json!({
        "__type__": "bytes",
        "data": encoded
    }))
}

fn handle_write(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
    capabilities: Option<&InitializeResponse>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct ContentBytes {
        #[serde(rename = "__type__")]
        _type_tag: String,
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

    ensure_capability(capabilities, &p.path, "write")?;
    client.write(&p.path, &content)?;
    if let Some(cache) = file_cache {
        cache.invalidate(&p.path);
    }
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
    serde_json::to_value(metadata)
        .map_err(|e| NexusClientError::InvalidResponse(format!("Serialization error: {}", e)))
}

fn handle_mkdir(
    params: &Value,
    client: &NexusClient,
    capabilities: Option<&InitializeResponse>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    ensure_capability(capabilities, &p.path, "mkdir")?;
    client.mkdir(&p.path)?;
    Ok(json!({}))
}

fn handle_delete(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
    capabilities: Option<&InitializeResponse>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    ensure_capability(capabilities, &p.path, "unlink")?;
    client.delete(&p.path)?;
    if let Some(cache) = file_cache {
        cache.invalidate(&p.path);
    }
    Ok(json!({}))
}

fn handle_rename(
    params: &Value,
    client: &NexusClient,
    file_cache: Option<&FileCache>,
    capabilities: Option<&InitializeResponse>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        old_path: String,
        new_path: String,
    }
    let p: P = extract_params(params)?;

    ensure_capability(capabilities, &p.old_path, "rename")?;
    ensure_capability(capabilities, &p.new_path, "rename")?;
    client.rename(&p.old_path, &p.new_path)?;
    if let Some(cache) = file_cache {
        cache.invalidate(&p.old_path);
        cache.invalidate(&p.new_path);
    }
    Ok(json!({}))
}

fn handle_exists(params: &Value, client: &NexusClient) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        path: String,
    }
    let p: P = extract_params(params)?;

    // #4056 R4: use the fallible variant so 401/403 / -32003 / -32004
    // surface as proper EACCES/EPERM in the JSON-RPC error response
    // instead of being silently folded into {"exists": false}, which
    // would let auth failures masquerade as missing paths.
    let exists = client.exists_result(&p.path)?;
    Ok(json!({ "exists": exists }))
}

async fn handle_cache_warm(
    params: Value,
    client: Arc<NexusClient>,
    file_cache: Option<Arc<FileCache>>,
) -> Result<Value, NexusClientError> {
    #[derive(Deserialize)]
    struct P {
        workspace_root: String,
        #[serde(default)]
        threshold_bytes: Option<usize>,
        #[serde(default)]
        budget_bytes: Option<usize>,
        #[serde(default)]
        concurrency: Option<usize>,
        /// When `true` (the default), the RPC blocks until hydration finishes
        /// and returns full HydrateStats — useful for tests and synchronous
        /// callers. When `false`, the RPC kicks off hydration on a detached
        /// tokio task and returns immediately with `{started: true}`. The
        /// production FUSE-mount trigger uses `wait=false` so the foreground
        /// client's serialized RPC socket isn't held for the whole BFS+
        /// fetch+admit cycle.
        #[serde(default = "default_wait")]
        wait: bool,
    }
    fn default_wait() -> bool {
        true
    }

    let p: P = serde_json::from_value(params)
        .map_err(|e| NexusClientError::InvalidResponse(format!("Invalid params: {}", e)))?;

    let cache = file_cache.ok_or_else(|| {
        NexusClientError::InvalidResponse("cache_warm requires --cache (FileCache disabled)".into())
    })?;

    let mut opts = crate::hydrate::HydrateOptions::new(p.workspace_root);
    if let Some(t) = p.threshold_bytes {
        opts.threshold_bytes = t;
    }
    if let Some(b) = p.budget_bytes {
        opts.budget_bytes = b;
    }
    if let Some(c) = p.concurrency {
        opts.concurrency = c;
    }

    if p.wait {
        let stats = crate::hydrate::hydrate_workspace(client, cache, opts).await;
        serde_json::to_value(&stats).map_err(|e| {
            NexusClientError::InvalidResponse(format!("failed to serialize hydrate stats: {}", e))
        })
    } else {
        // Fire-and-forget: spawn on the daemon's tokio runtime and return
        // immediately. The detached task uses the SAME FileCache instance as
        // the foreground client (no cross-process foyer corruption risk),
        // and the foreground RPC socket is freed for FUSE traffic.
        tokio::spawn(async move {
            let stats = crate::hydrate::hydrate_workspace(client, cache, opts).await;
            log::info!("cache_warm (async) finished: {:?}", stats);
        });
        Ok(json!({ "started": true }))
    }
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

        let result = handle_read(&json!({"path": "/daemon.txt"}), &client, None).unwrap();

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

        let result = handle_read(&json!({"path": "/daemon.txt"}), &client, None);

        assert!(result.is_err());
        let metrics = crate::metrics::render();
        assert!(metrics.contains("nexus_read_bytes_total{tier=\"error\"} 0"));
        assert!(metrics.contains("nexus_read_latency_seconds_count{tier=\"error\"} 1"));
    }

    #[test]
    fn daemon_read_fails_closed_when_stat_errors_with_cache_enabled() {
        // #4055 R4: with a cache present, a stat error must propagate; we
        // must NOT fall through to read_with_cache(gen=0) because that
        // could surface a cached entry past current authorization.
        let _guard = test_guard();
        crate::metrics::reset_for_tests();
        let mut server = Server::new();

        // Stat returns 403 — caller is no longer authorized to view the file.
        let _stat_mock = server
            .mock("POST", "/api/nfs/stat")
            .with_status(403)
            .with_body("forbidden")
            .create();

        // Read mock is configured but should NEVER be hit — fail closed
        // on stat error means we never get to the read step.
        let read_mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"__type__":"bytes","data":"YQ=="}}"#)
            .expect(0)
            .create();

        // Pre-populate cache with a gen=0 entry so the stat-fallback-to-gen0
        // bug would otherwise have served cached bytes.
        let dir = tempfile::tempdir().unwrap();
        let cfg = crate::cache::CacheConfig::new(
            dir.path().to_path_buf(),
            4 * 1024 * 1024,
            32 * 1024 * 1024,
            crate::cache::MAX_FILE_SIZE,
        )
        .unwrap();
        let cache =
            crate::cache::FileCache::new_with_config(&server.url(), "test-principal", cfg).unwrap();
        cache.put("/daemon.txt", b"warmed", Some("etag"), 0);

        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let result = handle_read(&json!({"path": "/daemon.txt"}), &client, Some(&cache));

        assert!(
            result.is_err(),
            "stat 403 must surface as an error, not be silently bypassed"
        );
        read_mock.assert(); // verifies the read mock was never invoked
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
            None,
            None,
        )
        .unwrap();

        assert!(crate::metrics::render().contains("nexus_write_backend_rpc_total 1"));
    }
}
