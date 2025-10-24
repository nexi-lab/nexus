#!/usr/bin/env python3
"""Check what backend the remote Nexus server is using."""

import sys
from pathlib import Path

# Add src to path for local development
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from nexus.remote import RemoteNexusFS

# Connect to remote server
print("Connecting to remote Nexus server at http://136.117.224.98:8080...")
nx = RemoteNexusFS(
    server_url="http://136.117.224.98:8080",
    api_key=None,
)
print("✓ Connected")

# Try to get info about the server
print("\n" + "=" * 70)
print("Checking Server Backend Configuration")
print("=" * 70)

# List root directory to see what's available
print("\nListing root directory:")
try:
    root_files = nx.list("/")
    print(f"Found {len(root_files)} items in root:")
    for item in root_files[:20]:
        print(f"  {item}")
    if len(root_files) > 20:
        print(f"  ... and {len(root_files) - 20} more")
except Exception as e:
    print(f"Error listing root: {e}")

# Test write and read to understand persistence
print("\n" + "=" * 70)
print("Testing File Persistence")
print("=" * 70)

test_path = "/.backend-test.txt"
test_content = b"Testing backend persistence"

try:
    print(f"\n1. Writing test file to {test_path}...")
    nx.write(test_path, test_content)
    print("   ✓ Write successful")

    print(f"\n2. Reading test file from {test_path}...")
    read_content = nx.read(test_path)
    print(f"   ✓ Read successful: {read_content.decode()}")

    print("\n3. Checking if file exists...")
    exists = nx.exists(test_path)
    print(f"   ✓ File exists: {exists}")

    print("\n" + "=" * 70)
    print("Backend Analysis")
    print("=" * 70)

    print("\nBased on the behavior:")
    print("- Files can be written and read successfully")
    print("- Files persist between operations")

    # Check if there are any GCS-specific paths or patterns
    if any(".gcs" in str(f) or "gcs" in str(f).lower() for f in root_files):
        print("- 🔍 Detected GCS-related paths in filesystem")
        print("- 📊 Backend: Likely using GCS (Google Cloud Storage)")
    else:
        print("- 📊 Backend: Cannot determine from paths alone")
        print("- 💡 Tip: Check server startup command or environment variables")

    print("\nTo definitively check the backend, you need to:")
    print("1. SSH into the server: gcloud compute ssh nexus-server --zone=us-west1-a")
    print("2. Check the running process: ps aux | grep nexus")
    print("3. Look for environment variables: cat /proc/$(pgrep -f nexus)/environ | tr '\\0' '\\n'")
    print("4. Or check the startup command in the service logs")

except Exception as e:
    print(f"Error during test: {e}")

print()
