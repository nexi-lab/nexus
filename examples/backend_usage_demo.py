"""Interactive demo showing how to use different backends with Nexus."""

import os
import tempfile

import nexus
from nexus.backends.gcs import GCSBackend
from nexus.backends.local import LocalBackend


def print_header(title):
    """Print a formatted header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


def print_step(step_num, description):
    """Print a step description."""
    print(f"\n[Step {step_num}] {description}")
    print("-" * 70)


def wait_for_user(prompt="Press Enter to continue..."):
    """Wait for user input."""
    input(f"\n{prompt}")


def demo_local_backend():
    """Example 1: Using LocalBackend (default behavior)."""
    print_header("Example 1: Local Backend (Default)")

    print("\nThis demo shows the default behavior of Nexus.")
    print("By default, Nexus uses LocalBackend to store files on your filesystem.")
    wait_for_user()

    with tempfile.TemporaryDirectory() as tmpdir:
        print_step(1, "Connect to Nexus (no backend specified)")
        print("Code: nx = nexus.connect()")
        nx = nexus.connect(config={"data_dir": tmpdir})
        print("✓ Connected with default LocalBackend")
        print(f"  Storage location: {tmpdir}")
        wait_for_user()

        print_step(2, "Write a file")
        print("Code: nx.write('/workspace/test.txt', b'Hello from LocalBackend!')")
        nx.write("/workspace/test.txt", b"Hello from LocalBackend!")
        print("✓ File written successfully")
        wait_for_user()

        print_step(3, "Read the file back")
        print("Code: content = nx.read('/workspace/test.txt')")
        content = nx.read("/workspace/test.txt")
        print(f"✓ Read from local storage: {content.decode()}")

        nx.close()
        wait_for_user("Press Enter to continue to next example...")


def demo_explicit_local_backend():
    """Example 2: Explicitly providing LocalBackend."""
    print_header("Example 2: Custom Local Backend")

    print("\nYou can also create a LocalBackend explicitly and pass it to Nexus.")
    print("This gives you more control over the storage location.")
    wait_for_user()

    with tempfile.TemporaryDirectory() as tmpdir:
        print_step(1, "Create LocalBackend instance")
        print(f"Code: local = LocalBackend(root_path='{tmpdir}')")
        local_backend = LocalBackend(root_path=tmpdir)
        print("✓ Created LocalBackend")
        print(f"  Storage location: {tmpdir}")
        wait_for_user()

        print_step(2, "Connect to Nexus with custom backend")
        print("Code: nx = nexus.connect(backend=local)")
        nx = nexus.connect(backend=local_backend, config={"data_dir": tmpdir})
        print("✓ Connected with custom LocalBackend")
        wait_for_user()

        print_step(3, "Write and read data")
        print("Code: nx.write('/workspace/data.json', b'{...}')")
        nx.write("/workspace/data.json", b'{"message": "explicit local"}')
        print("✓ Data written")
        content = nx.read("/workspace/data.json")
        print(f"✓ Read back: {content.decode()}")

        nx.close()
        wait_for_user("Press Enter to continue to GCS backend demo...")


def demo_gcs_backend():
    """Example 3: Using GCS Backend."""
    print_header("Example 3: GCS Backend (Cloud Storage)")

    print("\nNow let's use Google Cloud Storage as the backend!")
    print("This stores file content in the cloud while keeping metadata local.")

    # Check if GCS credentials are available
    bucket_name = os.environ.get("GCS_BUCKET_NAME", "ceranva")
    project_id = os.environ.get("GCP_PROJECT_ID")

    print("\n📋 Configuration:")
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path:
        print(f"  • Credentials: {creds_path}")
    else:
        print("  • Credentials: Application Default Credentials (gcloud auth)")
    print(f"  • Bucket: {bucket_name}")
    if project_id:
        print(f"  • Project ID: {project_id}")

    wait_for_user()

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            print_step(1, "Create GCS backend instance")
            print(f"Code: gcs = GCSBackend(bucket_name='{bucket_name}')")
            gcs_backend = GCSBackend(bucket_name=bucket_name, project_id=project_id)
            print("✓ GCS backend created successfully")
            wait_for_user()

            print_step(2, "Connect to Nexus with GCS backend")
            print("Code: nx = nexus.connect(backend=gcs)")
            nx = nexus.connect(backend=gcs_backend, config={"data_dir": tmpdir})
            print("✓ Connected with GCSBackend")
            print("\n  📦 Storage Architecture:")
            print("     • File content → GCS bucket (deduplicated)")
            print("     • Metadata → Local SQLite database")
            wait_for_user()

            print_step(3, "Write a file to cloud storage")
            test_data = b"Hello from GCS Backend! This is stored in the cloud."
            print("Code: nx.write('/workspace/cloud_file.txt', b'...')")
            print(f"  Content size: {len(test_data)} bytes")
            nx.write("/workspace/cloud_file.txt", test_data)
            print("✓ File written to GCS bucket")
            print("  (Check your GCS bucket - the file is actually there!)")
            wait_for_user()

            print_step(4, "Read the file back from cloud")
            print("Code: content = nx.read('/workspace/cloud_file.txt')")
            content = nx.read("/workspace/cloud_file.txt")
            print(f"✓ Read from GCS: {content.decode()}")

            # Verify it matches
            assert content == test_data
            print("✓ Content verification passed")
            wait_for_user()

            print_step(5, "List all files")
            print("Code: files = nx.list()")
            files = nx.list()
            print(f"✓ Files in filesystem: {files}")
            wait_for_user()

            print_step(6, "Clean up - delete the file")
            print("Code: nx.delete('/workspace/cloud_file.txt')")
            nx.delete("/workspace/cloud_file.txt")
            print("✓ File deleted from GCS")
            print("  (The file is removed from both GCS and local metadata)")

            nx.close()
            wait_for_user("Press Enter to see content deduplication demo...")

    except Exception as e:
        print(f"\n✗ GCS Backend demo failed: {e}")
        print("\n  This is expected if GCS credentials are not configured.")
        print("\n  To use GCS backend:")
        print("    1. Run: gcloud auth application-default login")
        print("    2. Or set: GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json")
        print("    3. Set: GCS_BUCKET_NAME=your-bucket-name (optional)")
        print("\n  Skipping GCS demo...")
        wait_for_user("Press Enter to continue...")


def demo_backend_comparison():
    """Example 4: Content Deduplication Demo."""
    print_header("Example 4: Content Deduplication (CAS)")

    print("\nBoth LocalBackend and GCSBackend use Content-Addressable Storage (CAS).")
    print("This means identical content is stored only ONCE, no matter how many")
    print("files reference it. Let's see this in action!")
    wait_for_user()

    with tempfile.TemporaryDirectory() as tmpdir:
        print_step(1, "Create a LocalBackend and connect")
        local = LocalBackend(root_path=tmpdir + "/local")
        nx_local = nexus.connect(backend=local, config={"data_dir": tmpdir + "/local"})
        print("✓ Connected to LocalBackend")
        wait_for_user()

        print_step(2, "Write the same content to two different files")
        test_content = b"This is the same content stored in different files"
        print(f"Content: {test_content.decode()}")
        print("\nCode:")
        print("  nx.write('/file1.txt', test_content)")
        print("  nx.write('/file2.txt', test_content)  # Same content!")

        nx_local.write("/file1.txt", test_content)
        print("✓ Wrote /file1.txt")

        nx_local.write("/file2.txt", test_content)
        print("✓ Wrote /file2.txt")
        wait_for_user()

        print_step(3, "Verify both files exist")
        print("Code: nx.exists('/file1.txt'), nx.exists('/file2.txt')")
        assert nx_local.exists("/file1.txt")
        assert nx_local.exists("/file2.txt")
        print("✓ Both files exist in the filesystem")
        wait_for_user()

        print_step(4, "Check physical storage")
        print("Even though we have 2 files, the content is stored only ONCE!")
        print("\n💡 How CAS works:")
        print("   1. Nexus computes SHA-256 hash of content")
        print("   2. Stores content using hash as filename")
        print("   3. Both /file1.txt and /file2.txt point to same hash")
        print("   4. Deleting one file doesn't delete the content (ref counting)")

        # Get the actual hash
        meta1 = nx_local.metadata.get("/file1.txt")
        meta2 = nx_local.metadata.get("/file2.txt")
        if meta1 and meta2:
            print(f"\n   /file1.txt → {meta1.etag[:16]}...")
            print(f"   /file2.txt → {meta2.etag[:16]}...")
            if meta1.etag == meta2.etag:
                print("   ✓ Same hash! Content stored only once!")

        wait_for_user()

        print_step(5, "Delete one file")
        print("Code: nx.delete('/file1.txt')")
        nx_local.delete("/file1.txt")
        print("✓ Deleted /file1.txt")
        print("  Content still exists (referenced by /file2.txt)")

        # Verify file2 still works
        content2 = nx_local.read("/file2.txt")
        assert content2 == test_content
        print("✓ /file2.txt still readable")
        wait_for_user()

        print_step(6, "Delete the second file")
        print("Code: nx.delete('/file2.txt')")
        nx_local.delete("/file2.txt")
        print("✓ Deleted /file2.txt")
        print("  NOW the content is actually deleted (ref count = 0)")

        nx_local.close()
        wait_for_user("Press Enter to finish demo...")


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print(" " * 20 + "🚀 Nexus Backend Usage Demo 🚀")
    print("=" * 80)
    print("\nThis interactive demo shows how to use different storage backends with Nexus.")
    print("You'll learn:")
    print("  • How to use LocalBackend (default)")
    print("  • How to use GCSBackend (cloud storage)")
    print("  • How Content-Addressable Storage (CAS) works")
    print("  • How deduplication saves storage space")

    wait_for_user("\nPress Enter to start the demo...")

    demo_local_backend()
    demo_explicit_local_backend()
    demo_gcs_backend()
    demo_backend_comparison()

    print("\n" + "=" * 80)
    print(" " * 30 + "✓ Demo Complete!")
    print("=" * 80)
    print("\n📚 Summary:")
    print("  1. Default: nexus.connect() uses LocalBackend automatically")
    print("  2. Custom: Pass backend=GCSBackend(...) to use cloud storage")
    print("  3. All backends support CAS for automatic deduplication")
    print("  4. Metadata is always stored locally in SQLite")
    print("  5. Same content is stored only once (saves space & costs)")
    print("\n💡 Next Steps:")
    print("  • Try the GCS backend with your own bucket")
    print("  • Check out examples/config_usage_demo.py for more features")
    print("  • Read docs/gcs_backend_usage.md for detailed documentation")
    print("\n" + "=" * 80)
