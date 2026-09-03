//! Write-to-searchable contract (#4736): the wire fields the HTTP layer
//! uses to make "write → index → query" a bounded, observable sequence.
//!
//! Pins:
//!
//! - `IndexDocuments` returns a strictly increasing `index_seq` and
//!   names content-skipped documents in `skipped_paths`;
//! - `Stats.last_index_seq` / `last_successful_index_at_ms` track the
//!   last COMMITTED mutation, and `pending` is 0 at rest;
//! - `NotifyFileChange{create|update}` is still a `"skipped"` ack that
//!   does NOT advance the sequence — the plugin has no text to index —
//!   while `delete` mutates and advances it;
//! - the sequence survives a plugin restart on the same data root.
//!
//! Uses the shared `tests/common` mock kernel — only the service
//! constructor needs a handle; IndexDocuments carries its text inline.

use std::sync::Arc;

use nexus_search_plugin::index_manager::IndexManager;
use nexus_search_plugin::search_proto::search_service_server::SearchService;
use nexus_search_plugin::search_proto::{
    DocumentInput, IndexDocumentsRequest, NotifyFileChangeRequest, QueryRequest, QueryType,
    StatsRequest,
};
use nexus_search_plugin::service::SearchServiceImpl;
use tempfile::TempDir;
use tonic::Request;

mod common;
use common::{handle_for, MockKernel};

/// IndexDocuments never touches the kernel (text travels inline); the
/// service constructor still needs a handle.  Leaked on purpose — the
/// tests are short-lived and the handle must outlive the service.
fn leak_kernel() -> *const MockKernel {
    Box::into_raw(Box::new(MockKernel::new()))
}

fn service_at(dir: &TempDir) -> SearchServiceImpl {
    SearchServiceImpl::builder(Arc::new(handle_for(leak_kernel())))
        .manager(Arc::new(IndexManager::with_root(dir.path().to_path_buf())))
        .no_expander()
        .no_peer_fanout()
        .no_context_generator()
        .build()
}

fn doc(path: &str, text: &str) -> DocumentInput {
    DocumentInput {
        path: path.to_string(),
        text: text.to_string(),
        mtime_ms: None,
        zone_id: String::new(),
    }
}

async fn index_docs(
    svc: &SearchServiceImpl,
    documents: Vec<DocumentInput>,
) -> nexus_search_plugin::search_proto::IndexDocumentsResponse {
    let resp = svc
        .index_documents(Request::new(IndexDocumentsRequest {
            documents,
            zone_id: String::new(),
            auth_token: String::new(),
        }))
        .await
        .expect("index_documents rpc")
        .into_inner();
    assert!(
        resp.error.is_none(),
        "index_documents error: {:?}",
        resp.error
    );
    resp
}

async fn stats(svc: &SearchServiceImpl) -> nexus_search_plugin::search_proto::StatsResponse {
    svc.stats(Request::new(StatsRequest::default()))
        .await
        .expect("stats rpc")
        .into_inner()
}

async fn notify(
    svc: &SearchServiceImpl,
    path: &str,
    change_type: &str,
) -> nexus_search_plugin::search_proto::NotifyFileChangeResponse {
    let resp = svc
        .notify_file_change(Request::new(NotifyFileChangeRequest {
            path: path.to_string(),
            change_type: change_type.to_string(),
            zone_id: String::new(),
            auth_token: String::new(),
        }))
        .await
        .expect("notify rpc")
        .into_inner();
    assert!(resp.error.is_none(), "notify error: {:?}", resp.error);
    resp
}

async fn keyword_hits(svc: &SearchServiceImpl, q: &str) -> Vec<String> {
    let resp = svc
        .query(Request::new(QueryRequest {
            q: q.to_string(),
            limit: 10,
            query_type: QueryType::Keyword as i32,
            ..Default::default()
        }))
        .await
        .expect("query rpc")
        .into_inner();
    assert!(resp.error.is_none(), "query error: {:?}", resp.error);
    resp.results.into_iter().map(|r| r.path).collect()
}

// ── Tests ─────────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn fresh_plugin_reports_nothing_indexed() {
    let dir = TempDir::new().unwrap();
    let svc = service_at(&dir);

    let s = stats(&svc).await;
    assert_eq!(s.last_index_seq, 0);
    assert_eq!(s.pending, 0);
    assert_eq!(s.last_successful_index_at_ms, 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn index_documents_returns_increasing_seq_and_stats_track_it() {
    let dir = TempDir::new().unwrap();
    let svc = service_at(&dir);

    let first = index_docs(&svc, vec![doc("/a.md", "alpha tenant document")]).await;
    assert_eq!(first.indexed_count, 1);
    assert_eq!(first.index_seq, 1, "first committed batch is seq 1");
    assert!(first.skipped_paths.is_empty());

    let s1 = stats(&svc).await;
    assert_eq!(s1.last_index_seq, first.index_seq);
    assert!(s1.last_successful_index_at_ms > 0);
    assert_eq!(s1.pending, 0, "nothing in flight once the RPC returned");

    let second = index_docs(&svc, vec![doc("/b.md", "bravo tenant document")]).await;
    assert!(second.index_seq > first.index_seq);

    let s2 = stats(&svc).await;
    assert_eq!(s2.last_index_seq, second.index_seq);
    assert!(s2.last_successful_index_at_ms >= s1.last_successful_index_at_ms);

    // The seq is proof of visibility: both documents are served.
    let hits = keyword_hits(&svc, "tenant").await;
    assert!(hits.contains(&"/a.md".to_string()), "hits: {hits:?}");
    assert!(hits.contains(&"/b.md".to_string()), "hits: {hits:?}");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn skipped_paths_name_the_content_skips() {
    let dir = TempDir::new().unwrap();
    let svc = service_at(&dir);

    let resp = index_docs(
        &svc,
        vec![
            doc("/real.md", "some real searchable text"),
            doc("/empty.md", ""),
            doc("/blank.md", "   \n\t "),
        ],
    )
    .await;
    assert_eq!(resp.indexed_count, 1);
    assert_eq!(resp.skipped_count, 2);
    let mut skipped = resp.skipped_paths.clone();
    skipped.sort();
    assert_eq!(
        skipped,
        vec!["/blank.md".to_string(), "/empty.md".to_string()]
    );
    // A batch that committed anything still gets a seq — the caller
    // reads per-path outcome from skipped_paths, not from seq == 0.
    assert!(resp.index_seq >= 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn notify_update_is_a_skipped_ack_that_does_not_advance_seq() {
    let dir = TempDir::new().unwrap();
    let svc = service_at(&dir);

    let indexed = index_docs(&svc, vec![doc("/a.md", "alpha")]).await;
    let before = stats(&svc).await;

    for change in ["create", "update", ""] {
        let ack = notify(&svc, "/a.md", change).await;
        assert_eq!(ack.status, "skipped", "change_type={change:?}");
        assert_eq!(ack.index_seq, 0, "a skipped ack carries no seq");
    }

    let after = stats(&svc).await;
    assert_eq!(after.last_index_seq, before.last_index_seq);
    assert_eq!(after.last_index_seq, indexed.index_seq);
    assert_eq!(
        after.last_successful_index_at_ms, before.last_successful_index_at_ms,
        "a no-op ack must not look like a successful index",
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn notify_delete_mutates_and_advances_seq() {
    let dir = TempDir::new().unwrap();
    let svc = service_at(&dir);

    let indexed = index_docs(&svc, vec![doc("/gone.md", "soon to be deleted")]).await;
    assert_eq!(
        keyword_hits(&svc, "deleted").await,
        vec!["/gone.md".to_string()]
    );

    let ack = notify(&svc, "/gone.md", "delete").await;
    assert_eq!(ack.status, "accepted");
    assert!(ack.index_seq > indexed.index_seq);

    let s = stats(&svc).await;
    assert_eq!(s.last_index_seq, ack.index_seq);
    assert!(keyword_hits(&svc, "deleted").await.is_empty());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn seq_survives_plugin_restart_on_same_root() {
    let dir = TempDir::new().unwrap();
    let last = {
        let svc = service_at(&dir);
        index_docs(&svc, vec![doc("/a.md", "alpha")]).await;
        index_docs(&svc, vec![doc("/b.md", "bravo")]).await;
        index_docs(&svc, vec![doc("/c.md", "charlie")])
            .await
            .index_seq
    };
    assert_eq!(last, 3);

    // Same data root, fresh process-equivalent.
    let svc = service_at(&dir);
    let s = stats(&svc).await;
    assert_eq!(
        s.last_index_seq, last,
        "a tenant holding seq {last} from before the restart must not see the counter rewind",
    );
    assert!(
        s.last_successful_index_at_ms > 0,
        "clock persisted with the seq"
    );

    let next = index_docs(&svc, vec![doc("/d.md", "delta")]).await;
    assert_eq!(next.index_seq, last + 1);
}
