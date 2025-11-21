#!/usr/bin/env python3
"""
Nexus S3 Connector Backend Demo (Python SDK)

Demonstrates S3 connector backend with direct path mapping:
- Mounting external S3 bucket with s3_connector backend type
- Writing files to actual paths (not CAS hashes)
- Reading files from S3 at real paths
- Directory operations on S3
- Workspace features still work
- Verifying files are browsable in S3 bucket

Key difference from CAS-based backends:
- CAS backend: Files stored as cas/ab/cd/hash... (content-addressed)
- S3 connector: Files stored at actual paths (e.g., workspace/file.txt)

Prerequisites:
1. S3 bucket created: aws s3 mb s3://YOUR-BUCKET-NAME
2. AWS authentication: aws configure
3. Nexus server running (optional, can run locally)

Usage:
    # With server:
    export NEXUS_URL=http://localhost:8080
    export NEXUS_API_KEY=your-api-key
    export S3_BUCKET_NAME=your-bucket-name
    export AWS_REGION=us-east-1
    python examples/python/s3_connector_demo.py

    # Local (no server):
    export S3_BUCKET_NAME=your-bucket-name
    export AWS_REGION=us-east-1
    python examples/python/s3_connector_demo.py --local
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


def check_aws_cli() -> bool:
    """Check if AWS CLI is available."""
    import subprocess

    try:
        subprocess.run(["aws", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def verify_s3_files(bucket_name: str, prefix: str) -> None:
    """Verify files exist in S3 bucket at actual paths."""
    import subprocess

    print_info("Listing files in S3 bucket...")
    print(f"$ aws s3 ls s3://{bucket_name}/{prefix}/ --recursive\n")

    try:
        result = subprocess.run(
            ["aws", "s3", "ls", f"s3://{bucket_name}/{prefix}/", "--recursive"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(result.stdout)
        print_success("Files stored at actual paths (not CAS hashes)!\n")

        # Read a file directly from S3
        print_info("Reading file directly from S3...")
        print(f"$ aws s3 cp s3://{bucket_name}/{prefix}/hello.txt -\n")

        result = subprocess.run(
            ["aws", "s3", "cp", f"s3://{bucket_name}/{prefix}/hello.txt", "-"],
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"  Content: {result.stdout}")
        print_success("External tools can read files directly!")

    except subprocess.CalledProcessError as e:
        print_error(f"Failed to verify S3 files: {e}")


def demo_with_server():
    """Run demo with Nexus server."""
    from nexus.sdk import connect

    print_section("Connecting to Nexus Server")

    nexus_url = os.getenv("NEXUS_URL")
    api_key = os.getenv("NEXUS_API_KEY")
    bucket_name = os.getenv("S3_BUCKET_NAME")
    region_name = os.getenv("AWS_REGION", "us-east-1")

    if not nexus_url:
        print_error("NEXUS_URL not set")
        print_info("Set: export NEXUS_URL=http://localhost:8080")
        sys.exit(1)

    if not api_key:
        print_error("NEXUS_API_KEY not set")
        print_info("Get API key: nexus admin create-api-key")
        sys.exit(1)

    if not bucket_name:
        print_error("S3_BUCKET_NAME not set")
        print_info("Set: export S3_BUCKET_NAME=your-bucket-name")
        sys.exit(1)

    print_info(f"Server: {nexus_url}")
    print_info(f"Bucket: s3://{bucket_name}")
    print_info(f"Region: {region_name}")

    # Connect to server using SDK (auto-discovers from env vars)
    nx = connect()
    print_success("Connected to Nexus server")

    # Mount S3 connector backend
    print_section("1. Mounting S3 Connector Backend")

    mount_point = "/workspace/s3"
    prefix = "nexus-demo"

    mount_config = {
        "bucket": bucket_name,
        "region_name": region_name,
        "prefix": prefix,
    }

    try:
        mount_id = nx.add_mount(
            mount_point=mount_point,
            backend_type="s3_connector",
            backend_config=mount_config,
            priority=10,
        )
        print_success(f"Mounted S3 connector at {mount_point}")
        print_info("  Backend type: s3_connector")
        print_info(f"  Bucket: s3://{bucket_name}")
        print_info(f"  Region: {region_name}")
        print_info(f"  Prefix: {prefix}/")
        print_info(f"  Mount ID: {mount_id}")
    except Exception as e:
        print_error(f"Failed to mount: {e}")
        sys.exit(1)

    # Write files
    print_section("2. Writing Files at Actual Paths")

    nx.write(f"{mount_point}/hello.txt", b"Hello from Nexus S3 Connector!")
    print_success(f"Wrote: {mount_point}/hello.txt")

    nx.write(
        f"{mount_point}/data.json",
        json.dumps({"type": "connector", "backend": "s3", "path_based": True}).encode(),
    )
    print_success(f"Wrote: {mount_point}/data.json")

    nx.mkdir(f"{mount_point}/subdir", parents=True)
    print_success(f"Created: {mount_point}/subdir")

    nx.write(f"{mount_point}/subdir/nested.txt", b"File in subdirectory")
    print_success(f"Wrote: {mount_point}/subdir/nested.txt")

    print_info("\nExpected S3 paths:")
    print(f"  s3://{bucket_name}/{prefix}/hello.txt")
    print(f"  s3://{bucket_name}/{prefix}/data.json")
    print(f"  s3://{bucket_name}/{prefix}/subdir/nested.txt")

    # Verify in S3
    if check_aws_cli():
        print_section("3. Verifying Files in S3 Bucket")
        verify_s3_files(bucket_name, prefix)
    else:
        print_section("3. Verifying Files (AWS CLI not available)")
        print_info("Install AWS CLI to verify files in S3 bucket")

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

    nx.rm(f"{mount_point}/hello.txt")
    nx.rm(f"{mount_point}/data.json")
    nx.rm(f"{mount_point}/subdir/nested.txt")
    nx.rmdir(f"{mount_point}/subdir")
    print_success("Cleaned up test files")

    nx.remove_mount(mount_point)
    print_success(f"Unmounted {mount_point}")


def demo_local():
    """Run demo locally (no server)."""
    from nexus.backends.s3_connector import S3ConnectorBackend
    from nexus.sdk import connect

    print_section("Running Local Demo (No Server)")

    bucket_name = os.getenv("S3_BUCKET_NAME")
    region_name = os.getenv("AWS_REGION", "us-east-1")

    if not bucket_name:
        print_error("S3_BUCKET_NAME not set")
        print_info("Set: export S3_BUCKET_NAME=your-bucket-name")
        sys.exit(1)

    print_info(f"Bucket: s3://{bucket_name}")
    print_info(f"Region: {region_name}")

    # Create local Nexus instance
    with tempfile.TemporaryDirectory() as tmpdir:
        nx = connect(config={"mode": "embedded", "data_dir": tmpdir})
        print_success("Created local NexusFS instance")

        # Create and add S3 connector backend
        print_section("1. Creating S3 Connector Backend")

        s3_backend = S3ConnectorBackend(
            bucket_name=bucket_name, region_name=region_name, prefix="nexus-demo"
        )
        print_success("Created S3 connector backend")
        print_info(f"  Backend name: {s3_backend.name}")
        print_info(f"  Bucket: s3://{bucket_name}")
        print_info(f"  Region: {region_name}")
        print_info("  Prefix: nexus-demo/")

        # Add mount
        nx.router.add_mount(mount_point="/workspace/s3", backend=s3_backend, priority=10)
        print_success("Mounted S3 connector at /workspace/s3")

        # Write files
        print_section("2. Writing Files to S3")

        nx.write("/workspace/s3/hello.txt", b"Hello from local Nexus!")
        print_success("Wrote: /workspace/s3/hello.txt")

        nx.write(
            "/workspace/s3/data.json",
            json.dumps({"local": True, "backend": "s3_connector"}).encode(),
        )
        print_success("Wrote: /workspace/s3/data.json")

        nx.mkdir("/workspace/s3/subdir", parents=True)
        nx.write("/workspace/s3/subdir/test.txt", b"Test file")
        print_success("Wrote: /workspace/s3/subdir/test.txt")

        print_info("\nFiles stored in S3 at:")
        print(f"  s3://{bucket_name}/nexus-demo/hello.txt")
        print(f"  s3://{bucket_name}/nexus-demo/data.json")
        print(f"  s3://{bucket_name}/nexus-demo/subdir/test.txt")

        # Verify in S3
        if check_aws_cli():
            print_section("3. Verifying Files in S3")
            verify_s3_files(bucket_name, "nexus-demo")
        else:
            print_section("3. Verification (AWS CLI not available)")
            print_info("Install AWS CLI to verify files")

        # Read files back
        print_section("4. Reading Files from S3")

        content = nx.read("/workspace/s3/hello.txt")
        print(f"  hello.txt: {content.decode()}")
        print_success("Read successfully")

        # List directory
        print_section("5. Directory Listing")

        files = nx.list("/workspace/s3")
        print("Files:")
        for f in files:
            print(f"  - {f}")
        print_success("Listed directory")

        # Cleanup
        print_section("6. Cleanup")

        nx.rm("/workspace/s3/hello.txt")
        nx.rm("/workspace/s3/data.json")
        nx.rm("/workspace/s3/subdir/test.txt")
        nx.rmdir("/workspace/s3/subdir")
        print_success("Cleaned up test files")

        print_info("\nS3 bucket cleanup (optional):")
        print(f"  aws s3 rm s3://{bucket_name}/nexus-demo/ --recursive")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Nexus S3 Connector Demo")
    parser.add_argument("--local", action="store_true", help="Run locally without server")
    args = parser.parse_args()

    print("╔" + "=" * 68 + "╗")
    print("║" + " " * 20 + "Nexus S3 Connector Backend Demo" + " " * 17 + "║")
    print("╚" + "=" * 68 + "╝\n")

    if args.local:
        demo_local()
    else:
        demo_with_server()

    # Summary
    print_section("Summary")
    print_success("S3 Connector Backend Demo Complete!\n")
    print_info("Key Takeaways:")
    print("  1. ✓ Files stored at actual paths in S3 (not content hashes)")
    print("  2. ✓ External tools can browse/access files in bucket")
    print("  3. ✓ Write-through storage (changes reflected immediately)")
    print("  4. ✓ Full Nexus workspace features via metadata layer")
    print("  5. ✓ No deduplication (each file independent)")
    print("  6. ✓ Perfect for mounting external S3 buckets\n")
    print_info("Comparison:")
    print("┌" + "─" * 68 + "┐")
    print("│ Feature            │ CAS Backend          │ Connector (s3_conn) │")
    print("├" + "─" * 68 + "┤")
    print("│ Storage Path       │ cas/ab/cd/hash...    │ actual/path.txt     │")
    print("│ Deduplication      │ Yes (ref counting)   │ No                  │")
    print("│ External Browsable │ No (hash-based)      │ Yes (path-based)    │")
    print("│ Use Case           │ Nexus-managed        │ External buckets    │")
    print("└" + "─" * 68 + "┘")


if __name__ == "__main__":
    main()
