#!/usr/bin/env python3
"""
Nexus GCS Connector Backend Demo (Python SDK)

Demonstrates GCS connector backend with direct path mapping:
- Mounting external GCS bucket with gcs_connector backend type
- Writing files to actual paths (not CAS hashes)
- Reading files from GCS at real paths
- Directory operations on GCS
- Workspace features still work
- Verifying files are browsable in GCS bucket

Key difference from regular GCS backend:
- GCS backend (CAS): Files stored as cas/ab/cd/hash... (content-addressed)
- GCS connector: Files stored at actual paths (e.g., workspace/file.txt)

Prerequisites:
1. GCS bucket created: gsutil mb gs://YOUR-BUCKET-NAME
2. GCS authentication: gcloud auth application-default login
3. Nexus server running (optional, can run locally)

Usage:
    # With server:
    export NEXUS_URL=http://localhost:8080
    export NEXUS_API_KEY=your-api-key
    export GCS_BUCKET_NAME=your-bucket-name
    python examples/python/gcs_connector_demo.py

    # Local (no server):
    export GCS_BUCKET_NAME=your-bucket-name
    export GCS_PROJECT_ID=your-project  # optional
    python examples/python/gcs_connector_demo.py --local
"""

import argparse
import json
import os
import sys
import tempfile


def print_section(title: str) -> None:
    """Print a section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70 + "\n")


def print_success(message: str) -> None:
    """Print success message."""
    print(f"✓ {message}")


def print_info(message: str) -> None:
    """Print info message."""
    print(f"ℹ {message}")


def print_error(message: str) -> None:
    """Print error message."""
    print(f"✗ {message}")


def check_gsutil() -> bool:
    """Check if gsutil is available."""
    import subprocess

    try:
        subprocess.run(["gsutil", "version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def verify_gcs_files(bucket_name: str, prefix: str) -> None:
    """Verify files exist in GCS bucket at actual paths."""
    import subprocess

    print_info("Listing files in GCS bucket...")
    print(f"$ gsutil ls -r gs://{bucket_name}/{prefix}/\n")

    try:
        result = subprocess.run(
            ["gsutil", "ls", "-r", f"gs://{bucket_name}/{prefix}/"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(result.stdout)
        print_success("Files stored at actual paths (not CAS hashes)!\n")

        # Read a file directly from GCS
        print_info("Reading file directly from GCS...")
        print(f"$ gsutil cat gs://{bucket_name}/{prefix}/hello.txt\n")

        result = subprocess.run(
            ["gsutil", "cat", f"gs://{bucket_name}/{prefix}/hello.txt"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"  Content: {result.stdout}")
        print_success("External tools can read files directly!")

    except subprocess.CalledProcessError as e:
        print_error(f"Failed to verify GCS files: {e}")


def demo_with_server():
    """Run demo with Nexus server."""
    from nexus.sdk import connect

    print_section("Connecting to Nexus Server")

    nexus_url = os.getenv("NEXUS_URL")
    api_key = os.getenv("NEXUS_API_KEY")
    bucket_name = os.getenv("GCS_BUCKET_NAME")
    project_id = os.getenv("GCS_PROJECT_ID")

    if not nexus_url:
        print_error("NEXUS_URL not set")
        print_info("Set: export NEXUS_URL=http://localhost:8080")
        sys.exit(1)

    if not api_key:
        print_error("NEXUS_API_KEY not set")
        print_info("Get API key: nexus admin create-api-key")
        sys.exit(1)

    if not bucket_name:
        print_error("GCS_BUCKET_NAME not set")
        print_info("Set: export GCS_BUCKET_NAME=your-bucket-name")
        sys.exit(1)

    print_info(f"Server: {nexus_url}")
    print_info(f"Bucket: gs://{bucket_name}")

    # Connect to server using SDK (auto-discovers from env vars)
    nx = connect()
    print_success("Connected to Nexus server")

    # Mount GCS connector backend
    print_section("1. Mounting GCS Connector Backend")

    mount_point = "/workspace/gcs"
    prefix = "nexus-demo"

    mount_config = {
        "bucket": bucket_name,
        "project_id": project_id,
        "prefix": prefix,
    }

    try:
        mount_id = nx.add_mount(
            mount_point=mount_point,
            backend_type="gcs_connector",
            backend_config=mount_config,
            priority=10,
        )
        print_success(f"Mounted GCS connector at {mount_point}")
        print_info("  Backend type: gcs_connector")
        print_info(f"  Bucket: gs://{bucket_name}")
        print_info(f"  Prefix: {prefix}/")
        print_info(f"  Mount ID: {mount_id}")
    except Exception as e:
        print_error(f"Failed to mount: {e}")
        sys.exit(1)

    # Write files
    print_section("2. Writing Files at Actual Paths")

    nx.write(f"{mount_point}/hello.txt", b"Hello from Nexus GCS Connector!")
    print_success(f"Wrote: {mount_point}/hello.txt")

    nx.write(
        f"{mount_point}/data.json",
        json.dumps({"type": "connector", "backend": "gcs", "path_based": True}).encode(),
    )
    print_success(f"Wrote: {mount_point}/data.json")

    nx.mkdir(f"{mount_point}/subdir", parents=True)
    print_success(f"Created: {mount_point}/subdir")

    nx.write(f"{mount_point}/subdir/nested.txt", b"File in subdirectory")
    print_success(f"Wrote: {mount_point}/subdir/nested.txt")

    print_info("\nExpected GCS paths:")
    print(f"  gs://{bucket_name}/{prefix}/hello.txt")
    print(f"  gs://{bucket_name}/{prefix}/data.json")
    print(f"  gs://{bucket_name}/{prefix}/subdir/nested.txt")

    # Verify in GCS
    if check_gsutil():
        print_section("3. Verifying Files in GCS Bucket")
        verify_gcs_files(bucket_name, prefix)
    else:
        print_section("3. Verifying Files (gsutil not available)")
        print_info("Install gsutil to verify files in GCS bucket")

    # Read files back
    print_section("4. Reading Files via Nexus")

    content = nx.read(f"{mount_point}/hello.txt")
    print(f"  Content: {content.decode()}")
    print_success("Read file successfully")

    json_content = nx.read(f"{mount_point}/data.json")
    print(f"  JSON: {json_content.decode()}")
    print_success("Read JSON file successfully")

    # Directory operations
    print_section("5. Directory Operations")

    files = nx.list(mount_point)
    print("Files in root:")
    for f in files:
        print(f"  - {f}")
    print_success("Listed directory")

    # Cleanup
    print_section("6. Cleanup")

    nx.delete(f"{mount_point}/hello.txt")
    nx.delete(f"{mount_point}/data.json")
    nx.delete(f"{mount_point}/subdir/nested.txt")
    nx.delete(f"{mount_point}/subdir")
    print_success("Cleaned up test files")

    nx.remove_mount(mount_point)
    print_success(f"Unmounted {mount_point}")


def demo_local():
    """Run demo locally (no server)."""
    from nexus.backends.gcs_connector import GCSConnectorBackend
    from nexus.sdk import connect

    print_section("Running Local Demo (No Server)")

    bucket_name = os.getenv("GCS_BUCKET_NAME")
    project_id = os.getenv("GCS_PROJECT_ID")

    if not bucket_name:
        print_error("GCS_BUCKET_NAME not set")
        print_info("Set: export GCS_BUCKET_NAME=your-bucket-name")
        sys.exit(1)

    print_info(f"Bucket: gs://{bucket_name}")

    # Create local Nexus instance
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = connect(config={"mode": "embedded", "data_dir": tmpdir})
        print_success("Created local NexusFS instance")

        # Create and add GCS connector backend
        print_section("1. Creating GCS Connector Backend")

        gcs_backend = GCSConnectorBackend(
            bucket_name=bucket_name, project_id=project_id, prefix="nexus-demo"
        )
        print_success("Created GCS connector backend")
        print_info(f"  Backend name: {gcs_backend.name}")
        print_info(f"  Bucket: gs://{bucket_name}")
        print_info("  Prefix: nexus-demo/")

        # Add mount
        nx.router.add_mount(mount_point="/workspace/gcs", backend=gcs_backend, priority=10)
        print_success("Mounted GCS connector at /workspace/gcs")

        # Write files
        print_section("2. Writing Files to GCS")

        nx.write("/workspace/gcs/hello.txt", b"Hello from local Nexus!")
        print_success("Wrote: /workspace/gcs/hello.txt")

        nx.write(
            "/workspace/gcs/data.json",
            json.dumps({"local": True, "backend": "gcs_connector"}).encode(),
        )
        print_success("Wrote: /workspace/gcs/data.json")

        nx.mkdir("/workspace/gcs/subdir", parents=True)
        nx.write("/workspace/gcs/subdir/test.txt", b"Test file")
        print_success("Wrote: /workspace/gcs/subdir/test.txt")

        print_info("\nFiles stored in GCS at:")
        print(f"  gs://{bucket_name}/nexus-demo/hello.txt")
        print(f"  gs://{bucket_name}/nexus-demo/data.json")
        print(f"  gs://{bucket_name}/nexus-demo/subdir/test.txt")

        # Verify in GCS
        if check_gsutil():
            print_section("3. Verifying Files in GCS")
            verify_gcs_files(bucket_name, "nexus-demo")
        else:
            print_section("3. Verification (gsutil not available)")
            print_info("Install gsutil to verify files")

        # Read files back
        print_section("4. Reading Files from GCS")

        content = nx.read("/workspace/gcs/hello.txt")
        print(f"  hello.txt: {content.decode()}")
        print_success("Read successfully")

        # List directory
        print_section("5. Directory Listing")

        files = nx.list("/workspace/gcs")
        print("Files:")
        for f in files:
            print(f"  - {f}")
        print_success("Listed directory")

        # Cleanup
        print_section("6. Cleanup")

        nx.rm("/workspace/gcs/hello.txt")
        nx.rm("/workspace/gcs/data.json")
        nx.rm("/workspace/gcs/subdir/test.txt")
        nx.rmdir("/workspace/gcs/subdir")
        print_success("Cleaned up test files")

        print_info("\nGCS bucket cleanup (optional):")
        print(f"  gsutil rm -r gs://{bucket_name}/nexus-demo/")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Nexus GCS Connector Demo")
    parser.add_argument("--local", action="store_true", help="Run locally without server")
    args = parser.parse_args()

    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 20 + "Nexus GCS Connector Backend Demo" + " " * 16 + "║")
    print("╚" + "=" * 68 + "╝\n")

    if args.local:
        demo_local()
    else:
        demo_with_server()

    # Summary
    print_section("Summary")
    print_success("GCS Connector Backend Demo Complete!\n")
    print_info("Key Takeaways:")
    print("  1. ✓ Files stored at actual paths in GCS (not content hashes)")
    print("  2. ✓ External tools can browse/access files in bucket")
    print("  3. ✓ Write-through storage (changes reflected immediately)")
    print("  4. ✓ Full Nexus workspace features via metadata layer")
    print("  5. ✓ No deduplication (each file independent)")
    print("  6. ✓ Perfect for mounting external GCS buckets\n")
    print_info("Comparison:")
    print("┌" + "─" * 68 + "┐")
    print("│ Feature            │ CAS Backend (gcs)    │ Connector (gcs_conn)│")
    print("├" + "─" * 68 + "┤")
    print("│ Storage Path       │ cas/ab/cd/hash...    │ actual/path.txt     │")
    print("│ Deduplication      │ Yes (ref counting)   │ No                  │")
    print("│ External Browsable │ No (hash-based)      │ Yes (path-based)    │")
    print("│ Use Case           │ Nexus-managed        │ External buckets    │")
    print("└" + "─" * 68 + "┘")


if __name__ == "__main__":
    main()
