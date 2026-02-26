//! Error handling tests for Nexus FUSE client (Issue 9A).
//!
//! Tests HTTP status code → NexusClientError → errno mapping using mockito.
//! Verifies that each HTTP error class produces the correct FUSE errno so that
//! retry logic (transient vs permanent) works correctly at the application layer.

use mockito::Server;
use nexus_fuse::client::NexusClient;
use nexus_fuse::error::NexusClientError;

#[test]
fn test_404_maps_to_not_found() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/read")
        .with_status(404)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "File not found"}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.read("/missing.txt");

    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(matches!(err, NexusClientError::NotFound(_)));
    assert_eq!(err.to_errno(), libc::ENOENT);
    assert!(err.is_not_found());
    assert!(!err.is_transient());
}

#[test]
fn test_429_maps_to_rate_limited() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/read")
        .with_status(429)
        .with_header("content-type", "application/json")
        .with_body(r#"{"error": "Rate limit exceeded"}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.read("/file.txt");

    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(matches!(err, NexusClientError::RateLimited));
    assert_eq!(err.to_errno(), libc::EBUSY);
    assert!(err.is_transient());
}

#[test]
fn test_500_maps_to_eio() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/read")
        .with_status(500)
        .with_header("content-type", "application/json")
        .with_body(r#"{"error": "Internal Server Error"}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.read("/file.txt");

    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(matches!(err, NexusClientError::ServerError { status: 500, .. }));
    assert_eq!(err.to_errno(), libc::EIO);
    assert!(err.is_transient());
}

#[test]
fn test_503_maps_to_server_error() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/stat")
        .with_status(503)
        .with_header("content-type", "application/json")
        .with_body(r#"{"error": "Service Unavailable"}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.stat("/file.txt");

    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(matches!(err, NexusClientError::ServerError { status: 503, .. }));
    assert_eq!(err.to_errno(), libc::EIO);
    assert!(err.is_transient());
}

#[test]
fn test_connection_refused_is_transient() {
    // Connect to a port that nothing listens on
    let client = NexusClient::new("http://127.0.0.1:1", "test-key", None).unwrap();
    let result = client.read("/file.txt");

    assert!(result.is_err());
    let err = result.unwrap_err();
    // reqwest connection errors are classified as transient via HttpError
    assert!(err.is_transient());
}

#[test]
fn test_rpc_not_found_in_body() {
    let mut server = Server::new();

    // Server returns 200 but the RPC body indicates "not found"
    let _m = server
        .mock("POST", "/api/nfs/read")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"File not found: /gone.txt"}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.read("/gone.txt");

    assert!(result.is_err());
    let err = result.unwrap_err();
    assert!(err.is_not_found());
    assert_eq!(err.to_errno(), libc::ENOENT);
}

#[test]
fn test_successful_stat() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/stat")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"size":42,"is_directory":false,"etag":"abc","modified_at":"2024-01-01T00:00:00Z"}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let meta = client.stat("/test.txt").unwrap();

    assert_eq!(meta.size, 42);
    assert!(!meta.is_directory);
    assert_eq!(meta.etag, Some("abc".to_string()));
}

#[test]
fn test_successful_read_with_base64() {
    use base64::{engine::general_purpose::STANDARD, Engine};
    let mut server = Server::new();

    let content = b"hello world";
    let encoded = STANDARD.encode(content);

    let _m = server
        .mock("POST", "/api/nfs/read")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(format!(
            r#"{{"jsonrpc":"2.0","id":1,"result":{{"__type__":"bytes","data":"{}"}}}}"#,
            encoded
        ))
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let data = client.read("/test.txt").unwrap();

    assert_eq!(data, b"hello world");
}

#[test]
fn test_successful_write() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/write")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.write("/test.txt", b"data");

    assert!(result.is_ok());
}

#[test]
fn test_successful_mkdir() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/mkdir")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.mkdir("/new-dir");

    assert!(result.is_ok());
}

#[test]
fn test_successful_delete() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/delete")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.delete("/to-delete.txt");

    assert!(result.is_ok());
}

#[test]
fn test_successful_rename() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/rename")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let result = client.rename("/old.txt", "/new.txt");

    assert!(result.is_ok());
}

#[test]
fn test_exists_returns_true() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/exists")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"exists":true}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    assert!(client.exists("/present.txt"));
}

#[test]
fn test_exists_returns_false() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/exists")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"exists":false}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    assert!(!client.exists("/absent.txt"));
}

#[test]
fn test_exists_on_error_returns_false() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/exists")
        .with_status(500)
        .with_body(r#"{"error":"boom"}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    assert!(!client.exists("/file.txt"));
}

#[test]
fn test_whoami_success() {
    let mut server = Server::new();

    let _m = server
        .mock("GET", "/api/auth/whoami")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"user_id":"u1","tenant_id":"t1","is_admin":true}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let info = client.whoami().unwrap();

    assert_eq!(info.user_id, Some("u1".to_string()));
    assert_eq!(info.tenant_id, Some("t1".to_string()));
    assert!(info.is_admin);
}

#[test]
fn test_whoami_unauthorized() {
    let mut server = Server::new();

    let _m = server
        .mock("GET", "/api/auth/whoami")
        .with_status(401)
        .with_body(r#"{"error":"Unauthorized"}"#)
        .create();

    let client = NexusClient::new(&server.url(), "bad-key", None).unwrap();
    let result = client.whoami();

    assert!(result.is_err());
}

#[test]
fn test_list_directory() {
    let mut server = Server::new();

    let _m = server
        .mock("POST", "/api/nfs/list")
        .with_status(200)
        .with_header("content-type", "application/json")
        .with_body(r#"{"jsonrpc":"2.0","id":1,"result":{"files":[{"path":"/hello.txt","is_directory":false,"size":5,"modified_at":null,"created_at":null}]}}"#)
        .create();

    let client = NexusClient::new(&server.url(), "test-key", None).unwrap();
    let entries = client.list("/").unwrap();

    assert_eq!(entries.len(), 1);
    assert_eq!(entries[0].name, "hello.txt");
    assert_eq!(entries[0].entry_type, "file");
    assert_eq!(entries[0].size, 5);
}
