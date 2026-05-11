//! Nexus HTTP client for communicating with the Nexus server.
//! Uses JSON-RPC style API.
//!
//! Async hyper/reqwest under the hood (#4056). Public methods stay sync
//! so FUSE callbacks and existing callers don't change, but internally
//! every request goes through one shared connection pool with HTTP
//! keep-alive enabled. The client owns a small multi-thread tokio
//! runtime that drives the futures; this lets concurrent reads from
//! distinct FUSE worker threads share one TCP/TLS connection pool
//! instead of each spinning a fresh `reqwest::blocking` runtime.

#![allow(dead_code)]

use crate::error::NexusClientError;
use log::debug;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE, IF_NONE_MATCH};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::future::Future;
use std::sync::OnceLock;
use std::time::Duration;
use tokio::runtime::Runtime;

/// Default connection-pool tunables. Tuned for FUSE fan-out: enough
/// idle slots that bursty parallel `read`/`stat` calls all hit a warm
/// connection instead of dialing a fresh one.
const POOL_MAX_IDLE_PER_HOST: usize = 64;
const POOL_IDLE_TIMEOUT_SECS: u64 = 60;
const TCP_KEEPALIVE_SECS: u64 = 30;
const REQUEST_TIMEOUT_SECS: u64 = 30;
const CONNECT_TIMEOUT_SECS: u64 = 5;

/// User information returned by whoami endpoint.
#[derive(Debug, Deserialize)]
pub struct UserInfo {
    #[serde(alias = "user_id", alias = "subject_id", default)]
    pub user_id: Option<String>,
    #[serde(default)]
    pub tenant_id: Option<String>,
    #[serde(default)]
    pub is_admin: bool,
    #[serde(default)]
    pub user: Option<serde_json::Value>,
}

/// File/directory entry from listing.
#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct FileEntry {
    pub name: String,
    #[serde(rename = "type")]
    pub entry_type: String,
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub created_at: Option<String>,
    #[serde(default)]
    pub updated_at: Option<String>,
}

/// File metadata from stat.
#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct FileMetadata {
    #[serde(default)]
    pub size: u64,
    #[serde(default)]
    pub gen: u64,
    #[serde(default)]
    pub etag: Option<String>,
    #[serde(default)]
    pub modified_at: Option<String>,
    #[serde(default)]
    pub is_directory: bool,
}

/// JSON-RPC response wrapper.
#[derive(Debug, Deserialize)]
struct JsonRpcResponse<T> {
    #[allow(dead_code)]
    jsonrpc: String,
    #[allow(dead_code)]
    id: Option<Value>,
    result: Option<T>,
    error: Option<JsonRpcError>,
}

#[derive(Debug, Deserialize)]
struct JsonRpcError {
    code: i32,
    message: String,
}

/// Response from read operations with ETag support.
#[derive(Debug)]
pub enum ReadResponse {
    /// Content was returned (possibly with ETag for caching).
    Content {
        content: Vec<u8>,
        etag: Option<String>,
    },
    /// Content not modified (304 response).
    NotModified,
}

/// Process-wide tokio runtime that drives every NexusClient HTTP
/// future. Stored in a `OnceLock` so it lives for the entire process
/// and never drops in an async context (which would panic). One
/// runtime serves every client/clone — the underlying reqwest::Client
/// already shares its connection pool across clones, so a shared
/// runtime adds no contention beyond the pool itself.
static HTTP_RUNTIME: OnceLock<Runtime> = OnceLock::new();

/// Build (once) and return a reference to the process-wide HTTP runtime.
/// Two worker threads is enough — per-call parallelism comes from many
/// FUSE worker threads each blocking on their own future, not from this
/// runtime fanning out internally. The runtime is `enable_all()` so
/// reqwest's hyper driver gets both the I/O and timer reactors.
fn http_runtime() -> &'static Runtime {
    HTTP_RUNTIME.get_or_init(|| {
        tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .thread_name("nexus-fuse-http")
            .build()
            .expect("failed to build nexus-fuse HTTP runtime")
    })
}

/// Nexus HTTP client.
///
/// `Clone` is cheap: `reqwest::Client` shares its connection pool via
/// an internal `Arc`.
#[derive(Clone)]
pub struct NexusClient {
    client: Client,
    base_url: String,
    api_key: String,
    agent_id: Option<String>,
}

impl NexusClient {
    /// Create a new Nexus client.
    pub fn new(
        base_url: &str,
        api_key: &str,
        agent_id: Option<String>,
    ) -> Result<Self, NexusClientError> {
        let client = Client::builder()
            .timeout(Duration::from_secs(REQUEST_TIMEOUT_SECS))
            .connect_timeout(Duration::from_secs(CONNECT_TIMEOUT_SECS))
            .pool_max_idle_per_host(POOL_MAX_IDLE_PER_HOST)
            .pool_idle_timeout(Some(Duration::from_secs(POOL_IDLE_TIMEOUT_SECS)))
            .tcp_keepalive(Some(Duration::from_secs(TCP_KEEPALIVE_SECS)))
            .no_proxy() // Disable proxy to avoid HTTP_PROXY interference
            .build()?;

        // Eagerly initialize the process-wide HTTP runtime so the
        // first request doesn't pay the build cost.
        let _ = http_runtime();

        Ok(Self {
            client,
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            agent_id,
        })
    }

    /// Run a future to completion on the shared process-wide HTTP runtime.
    ///
    /// # Contract
    ///
    /// The sync `read`/`write`/`stat`/… methods exist for **sync
    /// callsites only** — fuser callback threads, hydrate's
    /// `spawn_blocking` tasks, plain `#[test]` threads. Anywhere
    /// inside an `async fn` that is being polled by a tokio runtime,
    /// callers **must** use the `*_async` variants (`read_async`,
    /// `stat_async`, …). Calling a sync wrapper from an async task
    /// will trip tokio's `Cannot start a runtime from within a
    /// runtime` guard inside `Runtime::block_on` and panic the
    /// caller's task. This is intentional: the API surface stays
    /// minimal and the panic loudly catches the misuse. The
    /// "calling-from-async-panics" behavior is locked in by
    /// `tests/concurrent_stress_test.rs::sync_wrapper_panics_inside_async_task`
    /// (#4056 R2).
    ///
    /// # Why not auto-offload to a blocking thread?
    ///
    /// Tempting, but it would require every future returned by the
    /// `*_async` methods to be `Send + 'static`, forcing us to clone
    /// `self`'s state into the future. That's churn for a footgun
    /// the type system can't help us with anyway. The simpler
    /// contract — sync API for sync code, async API for async code,
    /// loud panic if you mix them — is what daemon refactors will
    /// reach for naturally.
    fn block_on<F: Future>(&self, fut: F) -> F::Output {
        http_runtime().block_on(fut)
    }

    /// Map HTTP status code to NexusClientError.
    fn status_to_error(status: reqwest::StatusCode, body: String) -> NexusClientError {
        match status.as_u16() {
            404 => NexusClientError::NotFound(body),
            429 => NexusClientError::RateLimited,
            500..=599 => NexusClientError::ServerError {
                status: status.as_u16(),
                message: body,
            },
            _ => NexusClientError::ServerError {
                status: status.as_u16(),
                message: body,
            },
        }
    }

    /// Build headers for requests.
    fn headers(&self) -> HeaderMap {
        let mut headers = HeaderMap::new();
        // Note: HeaderValue::from_str can fail on non-ASCII characters
        // In practice, API keys and agent IDs should be ASCII-safe
        if let Ok(auth_value) = HeaderValue::from_str(&format!("Bearer {}", self.api_key)) {
            headers.insert(AUTHORIZATION, auth_value);
        }
        headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
        if let Some(ref agent_id) = self.agent_id {
            if let Ok(agent_value) = HeaderValue::from_str(agent_id) {
                headers.insert("X-Agent-ID", agent_value);
            }
        }
        headers
    }

    /// Async core: call a JSON-RPC method.
    async fn rpc_call_async<T: for<'de> Deserialize<'de>>(
        &self,
        method: &str,
        params: Value,
    ) -> Result<T, NexusClientError> {
        let url = format!("{}/api/nfs/{}", self.base_url, method);

        let rpc_request = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params
        });
        debug!("POST {} {:?}", url, rpc_request);

        let resp = self
            .client
            .post(&url)
            .headers(self.headers())
            .json(&rpc_request)
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }

        let rpc_resp: JsonRpcResponse<T> = resp.json().await?;

        if let Some(err) = rpc_resp.error {
            // Classify by structured error code (rpc_types.py RPCErrorCode),
            // not message text. -32000 = FILE_NOT_FOUND per server contract.
            if err.code == -32000 {
                return Err(NexusClientError::NotFound(err.message));
            }
            return Err(NexusClientError::InvalidResponse(format!(
                "RPC error {}: {}",
                err.code, err.message
            )));
        }

        rpc_resp
            .result
            .ok_or_else(|| NexusClientError::InvalidResponse("no result in response".to_string()))
    }

    /// Async core: whoami.
    pub async fn whoami_async(&self) -> Result<UserInfo, NexusClientError> {
        let url = format!("{}/api/auth/whoami", self.base_url);
        debug!("GET {}", url);

        let resp = self
            .client
            .get(&url)
            .headers(self.headers())
            .send()
            .await?;

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }

        Ok(resp.json().await?)
    }

    /// Get current user info.
    pub fn whoami(&self) -> Result<UserInfo, NexusClientError> {
        self.block_on(self.whoami_async())
    }

    /// Async core: list.
    pub async fn list_async(&self, path: &str) -> Result<Vec<FileEntry>, NexusClientError> {
        // Use details=true, recursive=false to get entry types from server
        #[derive(Deserialize)]
        struct DetailedEntry {
            path: String,
            // The server sends `entry_type` as a numeric DT_* code (DT_REG=0,
            // DT_DIR=1, DT_MOUNT=2, ...). Older mocks/tests still use the
            // boolean `is_directory` field, so we accept both. (#4055 R6)
            #[serde(default)]
            entry_type: Option<u8>,
            #[serde(default)]
            is_directory: bool,
            #[serde(default)]
            size: u64,
            // These fields have complex nested types from the API, so we ignore them
            #[serde(default)]
            modified_at: serde_json::Value,
            #[serde(default)]
            created_at: serde_json::Value,
        }

        impl DetailedEntry {
            /// Only DT_DIR=1 and DT_MOUNT=2 are directory-like in the
            /// server's metadata contract (see src/nexus/contracts/metadata.py).
            /// DT_PIPE=3, DT_STREAM=4, DT_EXTERNAL_STORAGE=5, DT_LINK=6 are
            /// not normal directories; classifying them as such would have
            /// BFS try to list them (failing or returning unexpected shape)
            /// and would mislabel them in FUSE readdir output. (#4055 R10)
            /// `is_directory` is the legacy boolean fallback for older
            /// server responses / tests that don't emit `entry_type`.
            fn is_dir(&self) -> bool {
                if let Some(et) = self.entry_type {
                    et == 1 || et == 2
                } else {
                    self.is_directory
                }
            }
        }

        #[derive(Deserialize)]
        struct ListResult {
            files: Vec<DetailedEntry>,
        }

        let result: ListResult = self
            .rpc_call_async(
                "list",
                json!({
                    "path": path,
                    "recursive": false,
                    "details": true
                }),
            )
            .await?;

        // Convert to FileEntry objects - extract immediate children only
        let parent_prefix = if path == "/" { "/" } else { path };
        let mut seen_names = std::collections::HashSet::new();

        let entries = result
            .files
            .iter()
            .filter_map(|entry| {
                // Strip parent prefix to get relative path
                let relative = if path == "/" {
                    entry.path.strip_prefix('/').unwrap_or(&entry.path)
                } else {
                    entry
                        .path
                        .strip_prefix(parent_prefix)
                        .and_then(|s| s.strip_prefix('/'))
                        .unwrap_or(&entry.path)
                };

                // Get immediate child (first path component)
                let name = relative.split('/').next()?.to_string();
                if name.is_empty() {
                    return None;
                }

                // Deduplicate (same directory may appear multiple times from nested files)
                if !seen_names.insert(name.clone()) {
                    return None;
                }

                // If there are more path components, this is a directory
                let is_nested = relative.contains('/');
                let entry_type = if is_nested || entry.is_dir() {
                    "directory".to_string()
                } else {
                    "file".to_string()
                };

                Some(FileEntry {
                    name,
                    entry_type,
                    size: if is_nested { 0 } else { entry.size },
                    created_at: None, // Complex type from API, not used
                    updated_at: None, // Complex type from API, not used
                })
            })
            .collect();

        Ok(entries)
    }

    /// List directory contents.
    pub fn list(&self, path: &str) -> Result<Vec<FileEntry>, NexusClientError> {
        self.block_on(self.list_async(path))
    }

    /// Async core: stat.
    pub async fn stat_async(&self, path: &str) -> Result<FileMetadata, NexusClientError> {
        self.rpc_call_async("stat", json!({"path": path})).await
    }

    /// Get file/directory metadata.
    pub fn stat(&self, path: &str) -> Result<FileMetadata, NexusClientError> {
        self.block_on(self.stat_async(path))
    }

    /// Read file contents.
    pub fn read(&self, path: &str) -> Result<Vec<u8>, NexusClientError> {
        match self.read_with_etag(path, None)? {
            ReadResponse::Content { content, .. } => Ok(content),
            ReadResponse::NotModified => Err(NexusClientError::InvalidResponse(
                "Unexpected 304 without etag".to_string(),
            )),
        }
    }

    /// Async core: read with optional If-None-Match.
    pub async fn read_with_etag_async(
        &self,
        path: &str,
        if_none_match: Option<&str>,
    ) -> Result<ReadResponse, NexusClientError> {
        use base64::{engine::general_purpose::STANDARD, Engine};

        let url = format!("{}/api/nfs/read", self.base_url);

        let rpc_request = json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "read",
            "params": {"path": path}
        });

        let mut headers = self.headers();
        if let Some(etag) = if_none_match {
            headers.insert(
                IF_NONE_MATCH,
                HeaderValue::from_str(&format!("\"{}\"", etag)).unwrap(),
            );
        }

        debug!("POST {} (etag: {:?})", url, if_none_match);

        let resp = self
            .client
            .post(&url)
            .headers(headers)
            .json(&rpc_request)
            .send()
            .await?;

        // Handle 304 Not Modified
        if resp.status().as_u16() == 304 {
            debug!("Server returned 304 Not Modified for {}", path);
            return Ok(ReadResponse::NotModified);
        }

        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }

        // Extract ETag from response headers
        let etag = resp
            .headers()
            .get("etag")
            .and_then(|v| v.to_str().ok())
            .map(|s| s.trim_matches('"').to_string());

        // API returns {"__type__":"bytes","data":"base64..."} format
        #[derive(Deserialize)]
        struct BytesResult {
            #[serde(rename = "__type__")]
            type_tag: String,
            data: String, // base64 encoded
        }

        #[derive(Deserialize)]
        struct JsonRpcReadResponse {
            result: Option<BytesResult>,
            error: Option<JsonRpcError>,
        }

        let rpc_resp: JsonRpcReadResponse = resp.json().await?;

        if let Some(err) = rpc_resp.error {
            if err.code == -32000 {
                return Err(NexusClientError::NotFound(err.message));
            }
            return Err(NexusClientError::InvalidResponse(format!(
                "RPC error {}: {}",
                err.code, err.message
            )));
        }

        let result = rpc_resp.result.ok_or_else(|| {
            NexusClientError::InvalidResponse("no result in response".to_string())
        })?;
        let content = STANDARD.decode(&result.data).map_err(|e| {
            NexusClientError::InvalidResponse(format!("base64 decode error: {}", e))
        })?;

        Ok(ReadResponse::Content { content, etag })
    }

    /// Read file contents with ETag support for conditional requests.
    /// If `if_none_match` is provided and content hasn't changed, returns NotModified.
    pub fn read_with_etag(
        &self,
        path: &str,
        if_none_match: Option<&str>,
    ) -> Result<ReadResponse, NexusClientError> {
        self.block_on(self.read_with_etag_async(path, if_none_match))
    }

    /// Async core: write.
    pub async fn write_async(
        &self,
        path: &str,
        content: &[u8],
    ) -> Result<(), NexusClientError> {
        use base64::{engine::general_purpose::STANDARD, Engine};

        // API expects {"__type__": "bytes", "data": "base64..."} format
        let _: Value = self
            .rpc_call_async(
                "write",
                json!({
                    "path": path,
                    "content": {
                        "__type__": "bytes",
                        "data": STANDARD.encode(content)
                    }
                }),
            )
            .await?;
        Ok(())
    }

    /// Write file contents.
    pub fn write(&self, path: &str, content: &[u8]) -> Result<(), NexusClientError> {
        self.block_on(self.write_async(path, content))
    }

    /// Async core: mkdir.
    pub async fn mkdir_async(&self, path: &str) -> Result<(), NexusClientError> {
        let _: Value = self
            .rpc_call_async("mkdir", json!({"path": path}))
            .await?;
        Ok(())
    }

    /// Create directory.
    pub fn mkdir(&self, path: &str) -> Result<(), NexusClientError> {
        self.block_on(self.mkdir_async(path))
    }

    /// Async core: delete.
    pub async fn delete_async(&self, path: &str) -> Result<(), NexusClientError> {
        let _: Value = self
            .rpc_call_async("delete", json!({"path": path}))
            .await?;
        Ok(())
    }

    /// Delete file or directory.
    pub fn delete(&self, path: &str) -> Result<(), NexusClientError> {
        self.block_on(self.delete_async(path))
    }

    /// Async core: rename.
    pub async fn rename_async(
        &self,
        old_path: &str,
        new_path: &str,
    ) -> Result<(), NexusClientError> {
        let _: Value = self
            .rpc_call_async(
                "rename",
                json!({
                    "old_path": old_path,
                    "new_path": new_path
                }),
            )
            .await?;
        Ok(())
    }

    /// Rename/move file or directory.
    pub fn rename(&self, old_path: &str, new_path: &str) -> Result<(), NexusClientError> {
        self.block_on(self.rename_async(old_path, new_path))
    }

    /// Async core: exists.
    pub async fn exists_async(&self, path: &str) -> bool {
        #[derive(Deserialize)]
        struct ExistsResult {
            exists: bool,
        }

        match self
            .rpc_call_async::<ExistsResult>("exists", json!({"path": path}))
            .await
        {
            Ok(result) => result.exists,
            Err(_) => false,
        }
    }

    /// Check if path exists.
    pub fn exists(&self, path: &str) -> bool {
        self.block_on(self.exists_async(path))
    }
}
