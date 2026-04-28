//! X/Twitter Connector — pure Rust ObjectStore via reqwest + X API v2.
//!
//! Implements ObjectStore for X/Twitter using the v2 REST API.
//! Auth: OAuth2 bearer token (refreshable via `set_bearer_token()`).
//!
//! Virtual filesystem structure:
//!   /timeline/     — Home timeline
//!   /mentions/     — Mentions
//!   /posts/        — User's tweets
//!   /bookmarks/    — Saved tweets
//!   /search/{q}    — Search results
//!   /users/{name}  — User profiles
//!
//! `add_mount(backend_type="x")`

use kernel::abc::object_store::{ObjectStore, StorageError, WriteResult};
use kernel::kernel::OperationContext;
use std::io;

const X_API_BASE: &str = "https://api.x.com/2";

/// X/Twitter backend — tweet/timeline operations via X API v2.
pub(crate) struct XBackend {
    backend_name: String,
    bearer_token: parking_lot::RwLock<String>,
    runtime: tokio::runtime::Runtime,
}

impl XBackend {
    pub(crate) fn new(name: &str, bearer_token: &str) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            bearer_token: parking_lot::RwLock::new(bearer_token.to_string()),
            runtime,
        })
    }

    #[allow(dead_code)]
    pub(crate) fn set_bearer_token(&self, token: &str) {
        *self.bearer_token.write() = token.to_string();
    }

    fn token(&self) -> String {
        self.bearer_token.read().clone()
    }

    /// Resolve path to (endpoint_type, param).
    fn resolve_path(path: &str) -> (&str, &str) {
        let path = path.trim_matches('/');
        if path.is_empty() {
            return ("root", "");
        }

        if let Some(rest) = path.strip_prefix("timeline") {
            return ("timeline", rest.trim_matches('/'));
        }
        if let Some(rest) = path.strip_prefix("mentions") {
            return ("mentions", rest.trim_matches('/'));
        }
        if let Some(rest) = path.strip_prefix("posts") {
            return ("posts", rest.trim_matches('/'));
        }
        if let Some(rest) = path.strip_prefix("bookmarks") {
            return ("bookmarks", rest.trim_matches('/'));
        }
        if let Some(rest) = path.strip_prefix("search/") {
            return ("search", rest);
        }
        if let Some(rest) = path.strip_prefix("users/") {
            return ("users", rest);
        }

        ("unknown", path)
    }

    /// Authenticated GET request.
    async fn api_get(
        client: &reqwest::Client,
        url: &str,
        token: &str,
    ) -> Result<serde_json::Value, StorageError> {
        let resp = client
            .get(url)
            .header("Authorization", format!("Bearer {token}"))
            .send()
            .await
            .map_err(|e| StorageError::IOError(io::Error::other(format!("X API GET: {e}"))))?;

        let status = resp.status();
        if status == reqwest::StatusCode::UNAUTHORIZED {
            return Err(StorageError::IOError(io::Error::other(
                "X API: unauthorized — token may be expired",
            )));
        }
        if status == reqwest::StatusCode::NOT_FOUND {
            return Err(StorageError::NotFound("X API: resource not found".into()));
        }
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            return Err(StorageError::IOError(io::Error::other(format!(
                "X API {status}: {body}"
            ))));
        }

        resp.json()
            .await
            .map_err(|e| StorageError::IOError(io::Error::other(format!("X API JSON: {e}"))))
    }

    /// Authenticated POST request.
    async fn api_post(
        client: &reqwest::Client,
        url: &str,
        token: &str,
        body: &serde_json::Value,
    ) -> Result<serde_json::Value, StorageError> {
        let resp = client
            .post(url)
            .header("Authorization", format!("Bearer {token}"))
            .json(body)
            .send()
            .await
            .map_err(|e| StorageError::IOError(io::Error::other(format!("X API POST: {e}"))))?;

        let status = resp.status();
        if !status.is_success() {
            let body_text = resp.text().await.unwrap_or_default();
            return Err(StorageError::IOError(io::Error::other(format!(
                "X API POST {status}: {body_text}"
            ))));
        }

        resp.json()
            .await
            .map_err(|e| StorageError::IOError(io::Error::other(format!("X API JSON: {e}"))))
    }
}

impl ObjectStore for XBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        content: &[u8],
        _content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            return Err(StorageError::NotSupported(
                "X backend does not support offset writes (tweets are immutable)",
            ));
        }

        let token = self.token();
        // Parse content as JSON tweet payload
        let payload: serde_json::Value = serde_json::from_slice(content).map_err(|e| {
            StorageError::IOError(io::Error::other(format!("Invalid tweet JSON: {e}")))
        })?;

        // Extract text field (required)
        let text = payload
            .get("text")
            .and_then(|v| v.as_str())
            .ok_or_else(|| {
                StorageError::IOError(io::Error::other("Tweet payload must contain 'text' field"))
            })?;

        let url = format!("{X_API_BASE}/tweets");
        let body = serde_json::json!({ "text": text });

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let result = Self::api_post(&client, &url, &token, &body).await?;

            let tweet_id = result
                .get("data")
                .and_then(|d| d.get("id"))
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();

            Ok(WriteResult {
                content_id: tweet_id,
                version: String::new(),
                size: content.len() as u64,
            })
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let token = self.token();
        let (endpoint, param) = Self::resolve_path(content_id);

        self.runtime.block_on(async {
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .map_err(|e| {
                    StorageError::IOError(io::Error::other(format!("HTTP client: {e}")))
                })?;

            let result = match endpoint {
                "root" => {
                    let dirs = vec![
                        "timeline/",
                        "mentions/",
                        "posts/",
                        "bookmarks/",
                        "search/",
                        "users/",
                    ];
                    serde_json::to_value(dirs).unwrap()
                }
                "timeline" => {
                    // Need user ID first — get authenticated user
                    let me_url = format!("{X_API_BASE}/users/me");
                    let me = Self::api_get(&client, &me_url, &token).await?;
                    let user_id = me
                        .get("data")
                        .and_then(|d| d.get("id"))
                        .and_then(|v| v.as_str())
                        .ok_or_else(|| {
                            StorageError::IOError(io::Error::other("Cannot resolve user ID"))
                        })?;
                    let url = format!(
                        "{X_API_BASE}/users/{user_id}/tweets?max_results=100\
                         &tweet.fields=created_at,public_metrics,author_id"
                    );
                    Self::api_get(&client, &url, &token).await?
                }
                "mentions" => {
                    let me_url = format!("{X_API_BASE}/users/me");
                    let me = Self::api_get(&client, &me_url, &token).await?;
                    let user_id = me
                        .get("data")
                        .and_then(|d| d.get("id"))
                        .and_then(|v| v.as_str())
                        .ok_or_else(|| {
                            StorageError::IOError(io::Error::other("Cannot resolve user ID"))
                        })?;
                    let url = format!(
                        "{X_API_BASE}/users/{user_id}/mentions?max_results=100\
                         &tweet.fields=created_at,public_metrics,author_id"
                    );
                    Self::api_get(&client, &url, &token).await?
                }
                "posts" => {
                    if param.is_empty() {
                        // List user's tweets
                        let me_url = format!("{X_API_BASE}/users/me");
                        let me = Self::api_get(&client, &me_url, &token).await?;
                        let user_id = me
                            .get("data")
                            .and_then(|d| d.get("id"))
                            .and_then(|v| v.as_str())
                            .ok_or_else(|| {
                                StorageError::IOError(io::Error::other("Cannot resolve user ID"))
                            })?;
                        let url = format!(
                            "{X_API_BASE}/users/{user_id}/tweets?max_results=100\
                             &tweet.fields=created_at,public_metrics"
                        );
                        Self::api_get(&client, &url, &token).await?
                    } else {
                        // Get specific tweet by ID
                        let tweet_id = param.strip_suffix(".json").unwrap_or(param);
                        let url = format!(
                            "{X_API_BASE}/tweets/{tweet_id}\
                             ?tweet.fields=created_at,public_metrics,author_id"
                        );
                        Self::api_get(&client, &url, &token).await?
                    }
                }
                "bookmarks" => {
                    let me_url = format!("{X_API_BASE}/users/me");
                    let me = Self::api_get(&client, &me_url, &token).await?;
                    let user_id = me
                        .get("data")
                        .and_then(|d| d.get("id"))
                        .and_then(|v| v.as_str())
                        .ok_or_else(|| {
                            StorageError::IOError(io::Error::other("Cannot resolve user ID"))
                        })?;
                    let url = format!(
                        "{X_API_BASE}/users/{user_id}/bookmarks?max_results=100\
                         &tweet.fields=created_at,public_metrics,author_id"
                    );
                    Self::api_get(&client, &url, &token).await?
                }
                "search" => {
                    if param.is_empty() {
                        return Err(StorageError::NotFound(
                            "search requires a query parameter".into(),
                        ));
                    }
                    let encoded = urlencoding::encode(param);
                    let url = format!(
                        "{X_API_BASE}/tweets/search/recent?query={encoded}&max_results=100\
                         &tweet.fields=created_at,public_metrics,author_id"
                    );
                    Self::api_get(&client, &url, &token).await?
                }
                "users" => {
                    if param.is_empty() {
                        return Err(StorageError::NotFound(
                            "users requires a username parameter".into(),
                        ));
                    }
                    let username = param.strip_suffix(".json").unwrap_or(param);
                    let url = format!(
                        "{X_API_BASE}/users/by/username/{username}\
                         ?user.fields=created_at,public_metrics,description"
                    );
                    Self::api_get(&client, &url, &token).await?
                }
                _ => {
                    return Err(StorageError::NotFound(format!(
                        "Unknown X endpoint: {endpoint}/{param}"
                    )));
                }
            };

            serde_json::to_vec_pretty(&result).map_err(|e| {
                StorageError::IOError(io::Error::other(format!("JSON serialize: {e}")))
            })
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let token = self.token();
        let tweet_id = content_id;
        let url = format!("{X_API_BASE}/tweets/{tweet_id}");

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .delete(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("X delete: {e}"))))?;

            if !resp.status().is_success() {
                let body = resp.text().await.unwrap_or_default();
                return Err(StorageError::IOError(io::Error::other(format!(
                    "X delete failed: {body}"
                ))));
            }
            Ok(())
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        // Virtual directories — no-op
        Ok(())
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        Ok(())
    }

    fn list_dir(&self, path: &str) -> Result<Vec<String>, StorageError> {
        let (endpoint, _param) = Self::resolve_path(path);
        match endpoint {
            "root" => Ok(vec![
                "timeline/".into(),
                "mentions/".into(),
                "posts/".into(),
                "bookmarks/".into(),
                "search/".into(),
                "users/".into(),
            ]),
            "timeline" | "mentions" | "posts" | "bookmarks" => {
                // These are leaf endpoints — read_content returns JSON data, not listings
                Err(StorageError::NotSupported("X endpoint is not a directory"))
            }
            "search" | "users" => Err(StorageError::NotSupported(
                "X endpoint requires a parameter",
            )),
            _ => Err(StorageError::NotFound(format!(
                "Unknown endpoint: {endpoint}"
            ))),
        }
    }
}

mod urlencoding {
    pub fn encode(s: &str) -> String {
        let mut result = String::with_capacity(s.len() * 3);
        for b in s.bytes() {
            match b {
                b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                    result.push(b as char);
                }
                _ => {
                    result.push('%');
                    result.push_str(&format!("{b:02X}"));
                }
            }
        }
        result
    }
}
