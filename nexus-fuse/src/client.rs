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
use std::collections::HashMap;
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

/// POSIX-style filesystem capabilities advertised by `/api/vfs/initialize`.
#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct PosixCapabilities {
    #[serde(default)]
    pub read: Option<bool>,
    #[serde(default)]
    pub readdir: Option<bool>,
    #[serde(default)]
    pub stat: Option<bool>,
    #[serde(default)]
    pub write: Option<bool>,
    #[serde(default)]
    pub unlink: Option<bool>,
    #[serde(default)]
    pub mkdir: Option<bool>,
    #[serde(default)]
    pub rmdir: Option<bool>,
    #[serde(default)]
    pub rename: Option<bool>,
    #[serde(default)]
    pub glob: Option<bool>,
}

impl PosixCapabilities {
    fn capability(&self, capability: &str) -> Option<bool> {
        match capability {
            "read" => self.read,
            "readdir" => self.readdir,
            "stat" => self.stat,
            "write" => self.write,
            "unlink" => self.unlink,
            "mkdir" => self.mkdir,
            "rmdir" => self.rmdir,
            "rename" => self.rename,
            "glob" => self.glob,
            _ => None,
        }
    }
}

/// Per-mount backend capability metadata.
#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct BackendCapabilities {
    #[serde(default)]
    pub backend_name: String,
    #[serde(default)]
    pub backend_type: String,
    #[serde(default)]
    pub posix: PosixCapabilities,
    #[serde(default)]
    pub features: Vec<String>,
    #[serde(default)]
    pub extensions: Vec<String>,
    #[serde(default)]
    pub rust_native: bool,
    #[serde(default)]
    pub external: bool,
}

/// VFS capabilities advertised by the Nexus server.
#[derive(Debug, Deserialize, Serialize, Clone, Default)]
pub struct VfsCapabilities {
    #[serde(default)]
    pub posix: PosixCapabilities,
    #[serde(default)]
    pub backends: HashMap<String, BackendCapabilities>,
}

impl VfsCapabilities {
    pub fn capability_for_path(&self, path: &str, capability: &str) -> Option<bool> {
        let normalized = normalize_path(path);
        let mut best: Option<(usize, &PosixCapabilities)> = None;

        for (mount_point, backend) in &self.backends {
            let mount = normalize_path(mount_point);
            if path_is_within_mount(&normalized, &mount) {
                let replace = best
                    .map(|(best_len, _)| mount.len() > best_len)
                    .unwrap_or(true);
                if replace {
                    best = Some((mount.len(), &backend.posix));
                }
            }
        }

        if let Some((_, posix)) = best {
            return posix.capability(capability);
        }

        self.posix.capability(capability)
    }
}

/// Initialize response returned by `/api/vfs/initialize`.
#[derive(Debug, Deserialize, Serialize, Clone)]
pub struct InitializeResponse {
    pub server_name: String,
    pub server_version: String,
    pub protocol_version: String,
    pub capabilities: VfsCapabilities,
}

fn normalize_path(path: &str) -> String {
    let trimmed = path.trim();
    if trimmed.is_empty() || trimmed == "/" {
        return "/".to_string();
    }
    let with_root = if trimmed.starts_with('/') {
        trimmed.to_string()
    } else {
        format!("/{}", trimmed)
    };
    with_root.trim_end_matches('/').to_string()
}

fn path_is_within_mount(path: &str, mount: &str) -> bool {
    mount == "/" || path == mount || path.starts_with(&format!("{}/", mount.trim_end_matches('/')))
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

/// Response from read operations that decode content directly into a writer.
#[derive(Debug)]
pub enum ReadToWriterResponse {
    /// Content was written into the supplied writer.
    Content {
        bytes_written: u64,
        etag: Option<String>,
    },
    /// Content not modified (304 response).
    NotModified,
}

enum EncodedReadResponse {
    Content { data: String, etag: Option<String> },
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
    ///
    /// 401 → `AccessDenied` (credentials missing/rejected, EACCES).
    /// 403 → `PermissionDenied` (policy denied the operation, EPERM).
    /// These two are kept distinct so callers can pick correct
    /// remediation (re-auth vs. give up) and so FUSE surfaces the
    /// right errno (#4056 R3).
    fn status_to_error(status: reqwest::StatusCode, body: String) -> NexusClientError {
        match status.as_u16() {
            401 => NexusClientError::AccessDenied(body),
            403 => NexusClientError::PermissionDenied(body),
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

    /// Map a JSON-RPC error code from `rpc_types.py::RPCErrorCode` to
    /// a typed `NexusClientError`. Server contract (kept in sync with
    /// `src/nexus/contracts/rpc_types.py::RPCErrorCode`):
    ///
    /// | Code  | Constant          | Variant            | errno    |
    /// |-------|-------------------|--------------------|----------|
    /// | -32000 | FILE_NOT_FOUND   | NotFound           | ENOENT   |
    /// | -32001 | FILE_EXISTS      | AlreadyExists      | EEXIST   |
    /// | -32002 | INVALID_PATH     | InvalidPath        | EINVAL   |
    /// | -32003 | ACCESS_DENIED    | AccessDenied       | EACCES   |
    /// | -32004 | PERMISSION_ERROR | PermissionDenied   | EPERM    |
    /// | -32005 | VALIDATION_ERROR | ValidationError    | EINVAL   |
    /// | -32006 | CONFLICT         | Conflict           | EAGAIN   |
    ///
    /// Unknown codes fall through to `InvalidResponse` with the
    /// structured code preserved in the message so logs / triage
    /// retain the signal (#4056 R3 / R4).
    fn rpc_error_to_client_error(code: i32, message: String) -> NexusClientError {
        match code {
            -32000 => NexusClientError::NotFound(message),
            -32001 => NexusClientError::AlreadyExists(message),
            -32002 => NexusClientError::InvalidPath(message),
            -32003 => NexusClientError::AccessDenied(message),
            -32004 => NexusClientError::PermissionDenied(message),
            -32005 => NexusClientError::ValidationError(message),
            -32006 => NexusClientError::Conflict(message),
            other => NexusClientError::InvalidResponse(format!("RPC error {}: {}", other, message)),
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
            // Classify by structured error code per rpc_types.py
            // RPCErrorCode contract (-32000 NOT_FOUND, -32003 ACCESS_DENIED,
            // -32004 PERMISSION_ERROR). #4056 R3.
            return Err(Self::rpc_error_to_client_error(err.code, err.message));
        }

        rpc_resp
            .result
            .ok_or_else(|| NexusClientError::InvalidResponse("no result in response".to_string()))
    }

    /// Async core: whoami.
    pub async fn whoami_async(&self) -> Result<UserInfo, NexusClientError> {
        let url = format!("{}/api/auth/whoami", self.base_url);
        debug!("GET {}", url);

        let resp = self.client.get(&url).headers(self.headers()).send().await?;

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

    /// Async core: discover VFS capabilities from servers that support initialize.
    pub async fn capabilities_async(&self) -> Result<Option<InitializeResponse>, NexusClientError> {
        let url = format!("{}/api/vfs/initialize", self.base_url);
        debug!("GET {}", url);

        let resp = self.client.get(&url).headers(self.headers()).send().await?;
        if resp.status().as_u16() == 404 {
            return Ok(None);
        }
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(Self::status_to_error(status, text));
        }

        Ok(Some(resp.json().await?))
    }

    /// Discover VFS capabilities from servers that support the initialize endpoint.
    pub fn capabilities(&self) -> Result<Option<InitializeResponse>, NexusClientError> {
        self.block_on(self.capabilities_async())
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

        match self
            .read_encoded_with_etag_async(path, if_none_match)
            .await?
        {
            EncodedReadResponse::Content { data, etag } => {
                let content = STANDARD.decode(&data).map_err(|e| {
                    NexusClientError::InvalidResponse(format!("base64 decode error: {}", e))
                })?;
                Ok(ReadResponse::Content { content, etag })
            }
            EncodedReadResponse::NotModified => Ok(ReadResponse::NotModified),
        }
    }

    async fn read_encoded_with_etag_async(
        &self,
        path: &str,
        if_none_match: Option<&str>,
    ) -> Result<EncodedReadResponse, NexusClientError> {
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
            return Ok(EncodedReadResponse::NotModified);
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
            return Err(Self::rpc_error_to_client_error(err.code, err.message));
        }

        let result = rpc_resp.result.ok_or_else(|| {
            NexusClientError::InvalidResponse("no result in response".to_string())
        })?;
        Ok(EncodedReadResponse::Content {
            data: result.data,
            etag,
        })
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

    /// Async core: read with optional If-None-Match and decode into a writer.
    pub async fn read_with_etag_to_writer_async<W: std::io::Write>(
        &self,
        path: &str,
        if_none_match: Option<&str>,
        writer: &mut W,
    ) -> Result<ReadToWriterResponse, NexusClientError> {
        use base64::engine::general_purpose::STANDARD;
        use base64::read::DecoderReader;
        use std::io;

        match self
            .read_encoded_with_etag_async(path, if_none_match)
            .await?
        {
            EncodedReadResponse::Content { data, etag } => {
                let mut decoder = DecoderReader::new(data.as_bytes(), &STANDARD);
                let bytes_written = io::copy(&mut decoder, writer).map_err(|err| {
                    if err.kind() == io::ErrorKind::InvalidData {
                        NexusClientError::InvalidResponse(format!("base64 decode error: {}", err))
                    } else {
                        NexusClientError::Other(anyhow::anyhow!(
                            "failed to write read response into writer: {}",
                            err
                        ))
                    }
                })?;
                Ok(ReadToWriterResponse::Content {
                    bytes_written,
                    etag,
                })
            }
            EncodedReadResponse::NotModified => Ok(ReadToWriterResponse::NotModified),
        }
    }

    /// Read file contents with ETag support, decoding directly into a writer.
    pub fn read_with_etag_to_writer<W: std::io::Write>(
        &self,
        path: &str,
        if_none_match: Option<&str>,
        writer: &mut W,
    ) -> Result<ReadToWriterResponse, NexusClientError> {
        self.block_on(self.read_with_etag_to_writer_async(path, if_none_match, writer))
    }

    /// Async core: write.
    pub async fn write_async(&self, path: &str, content: &[u8]) -> Result<(), NexusClientError> {
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
        let _: Value = self.rpc_call_async("mkdir", json!({"path": path})).await?;
        Ok(())
    }

    /// Create directory.
    pub fn mkdir(&self, path: &str) -> Result<(), NexusClientError> {
        self.block_on(self.mkdir_async(path))
    }

    /// Async core: delete.
    pub async fn delete_async(&self, path: &str) -> Result<(), NexusClientError> {
        let _: Value = self.rpc_call_async("delete", json!({"path": path})).await?;
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

    /// Async core: existence check returning the raw server result.
    /// Preserves `AccessDenied`/`PermissionDenied` instead of folding
    /// them into `false`, so callers that need to distinguish
    /// "path is missing" from "you don't have permission to ask" can
    /// see the auth signal (#4056 R4).
    pub async fn exists_result_async(&self, path: &str) -> Result<bool, NexusClientError> {
        #[derive(Deserialize)]
        struct ExistsResult {
            exists: bool,
        }

        let result: ExistsResult = self.rpc_call_async("exists", json!({"path": path})).await?;
        Ok(result.exists)
    }

    /// Async core: best-effort exists. Treats *all* errors (including
    /// auth denials and network failures) as "does not exist". Kept
    /// because most callers really do want a boolean and don't care
    /// to discriminate; new callers that *do* care should use
    /// `exists_result_async`.
    pub async fn exists_async(&self, path: &str) -> bool {
        self.exists_result_async(path).await.unwrap_or(false)
    }

    /// Fallible existence check (#4056 R4). Use this from any path
    /// where confusing `not authorized` with `does not exist` would
    /// be wrong — e.g. daemon JSON-RPC responses that must carry the
    /// real errno back to the Python client.
    pub fn exists_result(&self, path: &str) -> Result<bool, NexusClientError> {
        self.block_on(self.exists_result_async(path))
    }

    /// Best-effort existence check. See `exists_async` for the
    /// error-swallowing caveat; `exists_result` is the fallible
    /// variant.
    pub fn exists(&self, path: &str) -> bool {
        self.block_on(self.exists_async(path))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::{engine::general_purpose::STANDARD, Engine};
    use mockito::Server;
    use std::io;

    #[test]
    fn read_with_etag_to_writer_decodes_content_into_writer() {
        let mut server = Server::new();
        let body = format!(
            r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
            STANDARD.encode(b"writer-data")
        );
        let _mock = server
            .mock("POST", "/api/nfs/read")
            .match_body(mockito::Matcher::Regex(
                r#""path"\s*:\s*"/data/file.bin""#.to_string(),
            ))
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_header("etag", r#""etag-1""#)
            .with_body(body)
            .create();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let mut output = Vec::new();

        let result = client
            .read_with_etag_to_writer("/data/file.bin", None, &mut output)
            .unwrap();

        assert_eq!(output, b"writer-data");
        match result {
            ReadToWriterResponse::Content {
                bytes_written,
                etag,
            } => {
                assert_eq!(bytes_written, b"writer-data".len() as u64);
                assert_eq!(etag.as_deref(), Some("etag-1"));
            }
            ReadToWriterResponse::NotModified => panic!("expected content"),
        }
    }

    #[test]
    fn read_with_etag_to_writer_preserves_writer_errors_as_io_failures() {
        struct FailingWriter;

        impl io::Write for FailingWriter {
            fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
                Err(io::Error::new(io::ErrorKind::Other, "disk full"))
            }

            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        let mut server = Server::new();
        let body = format!(
            r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
            STANDARD.encode(b"writer-data")
        );
        let _mock = server
            .mock("POST", "/api/nfs/read")
            .with_status(200)
            .with_header("content-type", "application/json")
            .with_body(body)
            .create();
        let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
        let mut output = FailingWriter;

        let err = client
            .read_with_etag_to_writer("/data/file.bin", None, &mut output)
            .unwrap_err();

        assert!(matches!(err, NexusClientError::Other(_)));
        assert_eq!(err.to_errno(), libc::EIO);
    }
}
