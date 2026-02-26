//! Integration test for Rust FUSE client against live Nexus server
//!
//! Run with: cargo test --test integration_test -- --nocapture
//!
//! Requires: Nexus server running on localhost:2026

use nexus_fuse::client::NexusClient;
use nexus_fuse::error::NexusClientError;

#[test]
#[ignore] // Run with: cargo test -- --ignored --nocapture
fn test_e2e_with_live_server() {
    println!("\n🧪 Starting E2E test with live Nexus server...\n");

    // Create client
    let client = NexusClient::new(
        "http://localhost:2026",
        "sk-test-key-123",
        None,
    )
    .expect("Failed to create client");

    println!("✓ Client created");

    // Test 1: Authentication
    println!("\n[Test 1] Authentication");
    match client.whoami() {
        Ok(user_info) => {
            println!("✓ Authentication successful");
            println!("  Admin: {}", user_info.is_admin);
        }
        Err(e) => {
            println!("✗ Authentication failed: {}", e);
            println!("  Error errno: {}", e.to_errno());
            panic!("Authentication test failed");
        }
    }

    // Test 2: Write file
    println!("\n[Test 2] Write file");
    let test_content = b"Hello from Rust E2E test!";
    match client.write("/rust-e2e-test.txt", test_content) {
        Ok(_) => println!("✓ Write successful"),
        Err(e) => {
            println!("✗ Write failed: {}", e);
            panic!("Write test failed");
        }
    }

    // Test 3: Read file
    println!("\n[Test 3] Read file");
    match client.read("/rust-e2e-test.txt") {
        Ok(content) => {
            println!("✓ Read successful");
            assert_eq!(&content, test_content, "Content mismatch");
            println!("  Content verified: {}", String::from_utf8_lossy(&content));
        }
        Err(e) => {
            println!("✗ Read failed: {}", e);
            panic!("Read test failed");
        }
    }

    // Test 4: List directory
    println!("\n[Test 4] List directory");
    match client.list("/") {
        Ok(files) => {
            println!("✓ List successful");
            println!("  Found {} files", files.len());

            // Verify our test file is in the list
            let found = files.iter().any(|f| f.name == "rust-e2e-test.txt");
            assert!(found, "Test file not found in directory listing");
            println!("  Test file found in listing ✓");
        }
        Err(e) => {
            println!("✗ List failed: {}", e);
            panic!("List test failed");
        }
    }

    // Test 5: Stat file
    println!("\n[Test 5] Stat file");
    match client.stat("/rust-e2e-test.txt") {
        Ok(metadata) => {
            println!("✓ Stat successful");
            println!("  Size: {} bytes", metadata.size);
            println!("  Is directory: {}", metadata.is_directory);
            assert_eq!(metadata.size as usize, test_content.len());
            assert!(!metadata.is_directory);
        }
        Err(e) => {
            println!("✗ Stat failed: {}", e);
            panic!("Stat test failed");
        }
    }

    // Test 6: Create directory
    println!("\n[Test 6] Create directory");
    match client.mkdir("/rust-test-dir") {
        Ok(_) => println!("✓ Mkdir successful"),
        Err(e) => {
            println!("✗ Mkdir failed: {}", e);
            panic!("Mkdir test failed");
        }
    }

    // Test 7: Rename file
    println!("\n[Test 7] Rename file");
    match client.rename("/rust-e2e-test.txt", "/rust-e2e-renamed.txt") {
        Ok(_) => {
            println!("✓ Rename successful");

            // Verify old path doesn't exist
            match client.exists("/rust-e2e-test.txt") {
                false => println!("  Old path removed ✓"),
                true => panic!("Old path still exists after rename"),
            }

            // Verify new path exists
            match client.exists("/rust-e2e-renamed.txt") {
                true => println!("  New path exists ✓"),
                false => panic!("New path doesn't exist after rename"),
            }
        }
        Err(e) => {
            println!("✗ Rename failed: {}", e);
            panic!("Rename test failed");
        }
    }

    // Test 8: Delete file
    println!("\n[Test 8] Delete file");
    match client.delete("/rust-e2e-renamed.txt") {
        Ok(_) => {
            println!("✓ Delete successful");

            // Verify file is gone
            match client.exists("/rust-e2e-renamed.txt") {
                false => println!("  File removed ✓"),
                true => panic!("File still exists after delete"),
            }
        }
        Err(e) => {
            println!("✗ Delete failed: {}", e);
            panic!("Delete test failed");
        }
    }

    // Test 9: Remove directory
    println!("\n[Test 9] Remove directory");
    match client.delete("/rust-test-dir") {
        Ok(_) => println!("✓ Rmdir successful"),
        Err(e) => {
            println!("✗ Rmdir failed: {}", e);
            panic!("Rmdir test failed");
        }
    }

    // Test 10: Error handling - 404
    println!("\n[Test 10] Error handling (404)");
    match client.read("/nonexistent-file.txt") {
        Ok(_) => panic!("Expected NotFound error, got success"),
        Err(e) => {
            println!("✓ Correctly returned error: {}", e);
            assert!(e.is_not_found(), "Expected NotFound error");
            assert_eq!(e.to_errno(), libc::ENOENT);
            println!("  Error type: NotFound ✓");
            println!("  Errno: ENOENT ✓");
        }
    }

    println!("\n🎉 All E2E tests passed!\n");
}
