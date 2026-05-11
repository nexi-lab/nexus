//! Minimal HTTP test to debug connection issues.
//!
//! `#[ignore]` so it only runs on demand (`cargo test -- --ignored`).
//! Uses the async `reqwest::Client` directly to match the production
//! path post-#4056.

use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE};
use reqwest::Client;

#[tokio::test]
#[ignore]
async fn test_raw_http_request() {
    println!("\nTesting raw HTTP request to Nexus server...\n");

    let client = Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .connect_timeout(std::time::Duration::from_secs(5))
        .http1_only() // Force HTTP/1.1
        .build()
        .expect("Failed to create HTTP client");

    let mut headers = HeaderMap::new();
    headers.insert(
        AUTHORIZATION,
        HeaderValue::from_str("Bearer sk-test-key-123").unwrap(),
    );
    headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));

    let url = "http://localhost:2026/api/auth/whoami";
    println!("Making GET request to: {}", url);

    let resp = client.get(url).headers(headers).send().await;

    match resp {
        Ok(response) => {
            println!("Got response!");
            println!("  Status: {}", response.status());
            println!("  Headers: {:?}", response.headers());
            let body = response.text().await.unwrap_or_default();
            println!("  Body: {}", body);
        }
        Err(e) => {
            println!("Request failed: {:?}", e);
            println!("  Error type: {:?}", e);
            if e.is_timeout() {
                println!("  This is a timeout error");
            }
            if e.is_connect() {
                println!("  This is a connection error");
            }
            panic!("HTTP request failed");
        }
    }
}
