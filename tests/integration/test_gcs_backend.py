"""Test script for GCS backend with real credentials."""

import os
import sys
from pathlib import Path

# Load environment variables from .env file
from dotenv import load_dotenv

from nexus.backends.gcs import GCSBackend

load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))


def test_gcs_backend() -> int:
    """Test GCS backend with real data."""

    # Configuration - Can be set via environment variables or command line args
    BUCKET_NAME = "ceranva"
    PROJECT_ID = os.environ.get("GCP_PROJECT_ID", None)

    # Allow command line override
    if len(sys.argv) > 1:
        BUCKET_NAME = sys.argv[1]
    if len(sys.argv) > 2:
        PROJECT_ID = sys.argv[2]

    print("\n" + "=" * 60)
    print("Testing GCS Backend")
    print("=" * 60)

    # Check if credentials are set
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if creds_path:
        print(f"✓ Using credentials from: {creds_path}")
        if not Path(creds_path).exists():
            print(f"✗ ERROR: Credentials file not found at {creds_path}")
            return 1
    else:
        print("✓ Using Application Default Credentials (gcloud auth)")

    print(f"✓ Bucket: {BUCKET_NAME}")
    if PROJECT_ID:
        print(f"✓ Project ID: {PROJECT_ID}")
    print()

    try:
        # Initialize backend
        print("1. Initializing GCS backend...")
        backend = GCSBackend(bucket_name=BUCKET_NAME, project_id=PROJECT_ID)
        print("   ✓ Backend initialized successfully\n")

        # Test 1: Write content
        print("2. Testing write_content()...")
        test_content = b"Hello from Nexus GCS Backend! This is a test file."
        content_hash = backend.write_content(test_content)
        print(f"   ✓ Content written with hash: {content_hash[:16]}...")
        print(f"   ✓ Full hash: {content_hash}\n")

        # Test 2: Check if content exists
        print("3. Testing content_exists()...")
        exists = backend.content_exists(content_hash)
        print(f"   ✓ Content exists: {exists}\n")

        # Test 3: Get content size
        print("4. Testing get_content_size()...")
        size = backend.get_content_size(content_hash)
        print(f"   ✓ Content size: {size} bytes (expected: {len(test_content)})\n")

        # Test 4: Get reference count
        print("5. Testing get_ref_count()...")
        ref_count = backend.get_ref_count(content_hash)
        print(f"   ✓ Reference count: {ref_count}\n")

        # Test 5: Write same content again (deduplication)
        print("6. Testing deduplication (write same content again)...")
        content_hash2 = backend.write_content(test_content)
        print(f"   ✓ Got same hash: {content_hash == content_hash2}")
        ref_count2 = backend.get_ref_count(content_hash)
        print(f"   ✓ Reference count increased: {ref_count} -> {ref_count2}\n")

        # Test 6: Read content
        print("7. Testing read_content()...")
        read_content = backend.read_content(content_hash)
        print("   ✓ Content read successfully")
        print(f"   ✓ Content matches: {read_content == test_content}\n")

        # Test 7: Create directory
        print("8. Testing mkdir()...")
        backend.mkdir("test_dir", parents=True, exist_ok=True)
        print("   ✓ Directory created\n")

        # Test 8: Check if directory exists
        print("9. Testing is_directory()...")
        is_dir = backend.is_directory("test_dir")
        print(f"   ✓ Is directory: {is_dir}\n")

        # Test 9: Delete content (decrement ref count)
        print("10. Testing delete_content() - first deletion...")
        backend.delete_content(content_hash)
        ref_count3 = backend.get_ref_count(content_hash)
        print(f"    ✓ Reference count decremented: {ref_count2} -> {ref_count3}\n")

        # Test 10: Delete content again (should actually delete)
        print("11. Testing delete_content() - final deletion...")
        backend.delete_content(content_hash)
        exists_after = backend.content_exists(content_hash)
        print("    ✓ Content deleted")
        print(f"    ✓ Content exists after deletion: {exists_after}\n")

        # Test 11: Remove directory
        print("12. Testing rmdir()...")
        backend.rmdir("test_dir")
        is_dir_after = backend.is_directory("test_dir")
        print("    ✓ Directory removed")
        print(f"    ✓ Directory exists after removal: {is_dir_after}\n")

        print("=" * 60)
        print("✓ All tests passed successfully!")
        print("=" * 60)

    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback

        print("\nFull traceback:")
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(test_gcs_backend())
