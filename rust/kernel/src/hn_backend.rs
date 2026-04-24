//! HackerNews Connector — pure Rust ObjectStore via reqwest + HN Firebase API.
//!
//! Read-only backend mapping HackerNews feeds to a virtual filesystem:
//!   /top/1.json ... N.json   — Top stories with comments
//!   /new/1.json ... N.json   — Newest stories
//!   /best/1.json ... N.json  — Best stories
//!   /ask/1.json ... N.json   — Ask HN posts
//!   /show/1.json ... N.json  — Show HN posts
//!   /jobs/1.json ... N.json  — Job listings
//!
//! No authentication required (public Firebase REST API).
//!
//! `add_mount(backend_type="hn")`

use crate::backend::{ObjectStore, StorageError, WriteResult};
use crate::kernel::OperationContext;
use std::io;

const HN_API_BASE: &str = "https://hacker-news.firebaseio.com/v0";
const MAX_COMMENTS_DEPTH: usize = 5;
const MAX_COMMENTS_TOTAL: usize = 100;

const VALID_FEEDS: &[&str] = &["top", "new", "best", "ask", "show", "jobs"];

/// HackerNews backend — read-only story/comment access via Firebase REST API.
pub(crate) struct HNBackend {
    backend_name: String,
    stories_per_feed: usize,
    include_comments: bool,
    runtime: tokio::runtime::Runtime,
}

impl HNBackend {
    pub(crate) fn new(
        name: &str,
        stories_per_feed: usize,
        include_comments: bool,
    ) -> Result<Self, io::Error> {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()?;
        let clamped = stories_per_feed.clamp(1, 30);
        Ok(Self {
            backend_name: name.to_string(),
            stories_per_feed: clamped,
            include_comments,
            runtime,
        })
    }

    /// Resolve a virtual path to (feed, rank).
    /// Returns (feed, None) for directory listing, (feed, Some(rank)) for a story.
    fn resolve_path(path: &str) -> Result<(&str, Option<usize>), StorageError> {
        let path = path.trim_matches('/');
        if path.is_empty() {
            return Ok(("", None));
        }

        let mut parts = path.splitn(3, '/');
        let first = parts.next().unwrap_or("");

        // Strip optional "hn" prefix
        let (feed_part, file_part) = if first == "hn" {
            let feed = parts.next().unwrap_or("");
            let file = parts.next();
            (feed, file)
        } else {
            (first, parts.next())
        };

        if feed_part.is_empty() {
            return Ok(("", None));
        }

        if !VALID_FEEDS.contains(&feed_part) {
            return Err(StorageError::NotFound(format!(
                "Unknown feed: {feed_part}. Valid: top, new, best, ask, show, jobs"
            )));
        }

        match file_part {
            None => Ok((feed_part, None)),
            Some(filename) => {
                let rank_str = filename.strip_suffix(".json").ok_or_else(|| {
                    StorageError::NotFound(format!("Invalid file: {filename}"))
                })?;
                let rank: usize = rank_str.parse().map_err(|_| {
                    StorageError::NotFound(format!("Invalid rank in: {filename}"))
                })?;
                if rank < 1 {
                    return Err(StorageError::NotFound(format!(
                        "Rank {rank} out of range"
                    )));
                }
                Ok((feed_part, Some(rank)))
            }
        }
    }

    /// Fetch a single HN item by ID.
    async fn fetch_item(
        client: &reqwest::Client,
        item_id: u64,
    ) -> Option<serde_json::Value> {
        let url = format!("{HN_API_BASE}/item/{item_id}.json");
        let resp = client.get(&url).send().await.ok()?;
        resp.json::<serde_json::Value>().await.ok()
    }

    /// Recursively fetch comments with depth/count limits.
    fn fetch_comments<'a>(
        client: &'a reqwest::Client,
        comment_ids: &'a [u64],
        depth: usize,
        total: &'a mut usize,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Vec<serde_json::Value>> + 'a>> {
        Box::pin(async move {
            if depth >= MAX_COMMENTS_DEPTH || *total >= MAX_COMMENTS_TOTAL || comment_ids.is_empty()
            {
                return Vec::new();
            }

            let remaining = MAX_COMMENTS_TOTAL - *total;
            let ids = &comment_ids[..comment_ids.len().min(remaining)];

            let mut comments = Vec::new();
            for &id in ids {
                if *total >= MAX_COMMENTS_TOTAL {
                    break;
                }
                if let Some(mut comment) = Self::fetch_item(client, id).await {
                    *total += 1;
                    // Recurse into replies
                    if let Some(kids) = comment.get("kids").and_then(|k| k.as_array()) {
                        let kid_ids: Vec<u64> =
                            kids.iter().filter_map(|v| v.as_u64()).collect();
                        let replies =
                            Self::fetch_comments(client, &kid_ids, depth + 1, total).await;
                        if !replies.is_empty() {
                            comment["replies"] =
                                serde_json::Value::Array(replies.into_iter().collect());
                        }
                    }
                    comments.push(comment);
                }
            }
            comments
        })
    }

    /// Fetch story IDs for a feed.
    async fn fetch_story_ids(
        client: &reqwest::Client,
        feed: &str,
    ) -> Result<Vec<u64>, StorageError> {
        let endpoint = match feed {
            "top" => "topstories",
            "new" => "newstories",
            "best" => "beststories",
            "ask" => "askstories",
            "show" => "showstories",
            "jobs" => "jobstories",
            _ => {
                return Err(StorageError::NotFound(format!(
                    "Unknown feed: {feed}"
                )))
            }
        };
        let url = format!("{HN_API_BASE}/{endpoint}.json");
        let resp = client
            .get(&url)
            .send()
            .await
            .map_err(|e| StorageError::IOError(io::Error::other(format!("HN fetch {feed}: {e}"))))?;
        let ids: Vec<u64> = resp
            .json()
            .await
            .map_err(|e| StorageError::IOError(io::Error::other(format!("HN JSON: {e}"))))?;
        Ok(ids)
    }

    /// Fetch a story by feed rank, with optional nested comments.
    fn fetch_story(&self, feed: &str, rank: usize) -> Result<Vec<u8>, StorageError> {
        self.runtime.block_on(async {
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()
                .map_err(|e| StorageError::IOError(io::Error::other(format!("HTTP client: {e}"))))?;

            let story_ids = Self::fetch_story_ids(&client, feed).await?;
            if rank < 1 || rank > story_ids.len() {
                return Err(StorageError::NotFound(format!(
                    "Rank {rank} out of range (1-{})",
                    story_ids.len()
                )));
            }

            let story_id = story_ids[rank - 1];
            let mut story = Self::fetch_item(&client, story_id)
                .await
                .ok_or_else(|| StorageError::NotFound(format!("Story {story_id} not found")))?;

            // Add rank/feed metadata
            story["_rank"] = serde_json::Value::from(rank as u64);
            story["_feed"] = serde_json::Value::from(feed);

            // Fetch comments if requested
            if self.include_comments {
                if let Some(kids) = story.get("kids").and_then(|k| k.as_array()) {
                    let kid_ids: Vec<u64> = kids.iter().filter_map(|v| v.as_u64()).collect();
                    let mut total = 0usize;
                    let comments =
                        Self::fetch_comments(&client, &kid_ids, 0, &mut total).await;
                    story["comments"] = serde_json::Value::Array(comments);
                } else {
                    story["comments"] = serde_json::Value::Array(Vec::new());
                }
            } else {
                story["comments"] = serde_json::Value::Array(Vec::new());
            }

            serde_json::to_vec_pretty(&story)
                .map_err(|e| StorageError::IOError(io::Error::other(format!("JSON serialize: {e}"))))
        })
    }
}

impl ObjectStore for HNBackend {
    fn name(&self) -> &str {
        &self.backend_name
    }

    fn write_content(
        &self,
        _content: &[u8],
        _content_id: &str,
        _ctx: &OperationContext,
        _offset: u64,
    ) -> Result<WriteResult, StorageError> {
        Err(StorageError::NotSupported(
            "HN backend is read-only (HackerNews API does not support posting)",
        ))
    }

    fn read_content(
        &self,
        _content_id: &str,
        backend_path: &str,
        _ctx: &OperationContext,
    ) -> Result<Vec<u8>, StorageError> {
        let (feed, rank) = Self::resolve_path(backend_path)?;

        if feed.is_empty() {
            // Root listing
            let feeds: Vec<&str> = VALID_FEEDS.to_vec();
            return serde_json::to_vec(&feeds)
                .map_err(|e| StorageError::IOError(io::Error::other(e.to_string())));
        }

        match rank {
            Some(r) => self.fetch_story(feed, r),
            None => {
                // Feed directory — return list of story filenames
                let files: Vec<String> = (1..=self.stories_per_feed)
                    .map(|i| format!("{i}.json"))
                    .collect();
                serde_json::to_vec(&files)
                    .map_err(|e| StorageError::IOError(io::Error::other(e.to_string())))
            }
        }
    }

    fn delete_content(&self, _content_id: &str) -> Result<(), StorageError> {
        Err(StorageError::NotSupported(
            "HN backend is read-only (HackerNews API does not support deletion)",
        ))
    }

    fn delete_file(&self, _path: &str) -> Result<(), StorageError> {
        Err(StorageError::NotSupported(
            "HN backend is read-only",
        ))
    }

    fn mkdir(&self, _path: &str, _parents: bool, _exist_ok: bool) -> Result<(), StorageError> {
        // Virtual directories — no-op
        Ok(())
    }

    fn rmdir(&self, _path: &str, _recursive: bool) -> Result<(), StorageError> {
        Err(StorageError::NotSupported(
            "HN backend has a fixed virtual structure",
        ))
    }
}
