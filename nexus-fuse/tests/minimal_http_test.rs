//! Minimal HTTP test to debug connection issues

use reqwest::blocking::Client;
use reqwest::header::{HeaderMap, HeaderValue, AUTHORIZATION, CONTENT_TYPE};

#[test]
#[ignore]
fn test_raw_http_request() {
    println!("\n🧪 Testing raw HTTP request to Nexus server...\n");

    let client = Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .connect_timeout(std::time::Duration::from_secs(5))
        .http1_only()  // Force HTTP/1.1
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

    let resp = client.get(url).headers(headers).send();

    match resp {
        Ok(response) => {
            println!("✓ Got response!");
            println!("  Status: {}", response.status());
            println!("  Headers: {:?}", response.headers());
            let body = response.text().unwrap_or_default();
            println!("  Body: {}", body);
        }
        Err(e) => {
            println!("✗ Request failed: {:?}", e);
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
