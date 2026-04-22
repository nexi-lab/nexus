//! Slack Connector — pure Rust ObjectStore via reqwest + Slack Web API (§10 D6).
//!
//! Implements ObjectStore for Slack using Slack Web API (REST).
//! Auth: OAuth2 bot token.
//! Messages map to content blobs; channels map to directories.
//!
//! `add_mount(backend_type="slack")`

#![allow(dead_code)]

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use std::io;

const SLACK_API: &str = "https://slack.com/api";

/// Slack backend — message/channel operations via Web API.
pub(crate) struct SlackBackend {
    backend_name: String,
    bot_token: parking_lot::RwLock<String>,
    /// Default channel ID for writes without explicit channel.
    default_channel: String,
    runtime: tokio::runtime::Runtime,
}

impl SlackBackend {
    pub(crate) fn new(
        name: &str,
        bot_token: &str,
        default_channel: &str,
    ) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        Ok(Self {
            backend_name: name.to_string(),
            bot_token: parking_lot::RwLock::new(bot_token.to_string()),
            default_channel: default_channel.to_string(),
            runtime,
        })
    }

    pub(crate) fn set_bot_token(&self, token: &str) {
        *self.bot_token.write() = token.to_string();
    }

    fn token(&self) -> String {
        self.bot_token.read().clone()
    }
}

impl ObjectStore for SlackBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        content: &[u8],
        content_id: &str,
        _ctx: &OperationContext,
        offset: u64,
    ) -> Result<WriteResult, StorageError> {
        if offset != 0 {
            return Err(StorageError::NotSupported(
                "slack backend does not support offset writes (messages are immutable)",
            ));
        }
        let token = self.token();
        let text = String::from_utf8_lossy(content).to_string();
        let channel = if content_id.is_empty() {
            &self.default_channel
        } else {
            content_id
        };
        let url = format!("{}/chat.postMessage", SLACK_API);
        let body = serde_json::json!({
            "channel": channel,
            "text": text,
        });
        let size = content.len() as u64;

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .json(&body)
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Slack post: {e}"))))?;
            let result: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Slack JSON: {e}"))))?;
            if result.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                let err = result
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Slack error: {err}"
                ))));
            }
            let ts = result
                .get("ts")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string();
            Ok(WriteResult {
                content_id: ts,
                version: String::new(),
                size,
            })
        })
    }

    fn read_content(
        &self,
        content_id: &str,
        _backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let token = self.token();
        // content_id format: "channel:ts" or just channel (reads history)
        let (channel, ts) = if let Some(pos) = content_id.find(':') {
            (&content_id[..pos], Some(&content_id[pos + 1..]))
        } else {
            (content_id, None)
        };

        let url = if let Some(ts) = ts {
            // Single message via conversations.history with latest=ts, limit=1
            format!(
                "{}/conversations.history?channel={}&latest={}&limit=1&inclusive=true",
                SLACK_API, channel, ts
            )
        } else {
            // Channel history
            format!(
                "{}/conversations.history?channel={}&limit=100",
                SLACK_API, channel
            )
        };

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .get(&url)
                .header("Authorization", format!("Bearer {token}"))
                .send()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Slack GET: {e}"))))?;
            let result: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Slack JSON: {e}"))))?;
            if result.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                let err = result
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                return Err(StorageError::NotFound(format!("Slack: {err}")));
            }
            Ok(serde_json::to_vec(&result.get("messages")).unwrap_or_default())
        })
    }

    fn delete_content(&self, content_id: &str) -> Result<(), StorageError> {
        let token = self.token();
        // content_id format: "channel:ts"
        let (channel, ts) = content_id
            .split_once(':')
            .ok_or_else(|| StorageError::NotFound("invalid content_id: need channel:ts".into()))?;
        let url = format!("{}/chat.delete", SLACK_API);
        let body = serde_json::json!({
            "channel": channel,
            "ts": ts,
        });

        self.runtime.block_on(async {
            let client = reqwest::Client::new();
            let resp = client
                .post(&url)
                .header("Authorization", format!("Bearer {token}"))
                .json(&body)
                .send()
                .await
                .map_err(|e| {
                    StorageError::IOError(io::Error::other(format!("Slack delete: {e}")))
                })?;
            let result: serde_json::Value = resp
                .json()
                .await
                .map_err(|e| StorageError::IOError(io::Error::other(format!("Slack JSON: {e}"))))?;
            if result.get("ok").and_then(|v| v.as_bool()) != Some(true) {
                let err = result
                    .get("error")
                    .and_then(|v| v.as_str())
                    .unwrap_or("unknown");
                return Err(StorageError::IOError(io::Error::other(format!(
                    "Slack delete: {err}"
                ))));
            }
            Ok(())
        })
    }

    fn delete_file(&self, path: &str) -> Result<(), StorageError> {
        self.delete_content(path)
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        // Slack channels are created separately (not via mkdir)
        Ok(())
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        Ok(())
    }
}
