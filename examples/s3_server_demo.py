#!/usr/bin/env python3
"""Demo: Using Nexus with S3-compatible API.

This example demonstrates:
1. Starting a Nexus HTTP server with S3-compatible API
2. Using boto3 to interact with Nexus as if it were S3
3. Using requests with manual SigV4 signing

Requirements:
    pip install boto3 requests
"""

import time
from threading import Thread

import boto3

import nexus
from nexus.server.api import NexusHTTPServer
from nexus.server.auth import SigV4Validator, create_simple_credentials_store


def start_server_background(host="localhost", port=8080):
    """Start Nexus HTTP server in background thread.

    Args:
        host: Server host
        port: Server port

    Returns:
        Tuple of (server, nx_filesystem)
    """
    # Server credentials
    access_key = "demo-key"
    secret_key = "demo-secret"

    # Create Nexus filesystem
    nx = nexus.connect(config={"data_dir": "./demo-nexus-data"})

    # Create server
    credentials_store = create_simple_credentials_store(access_key, secret_key)
    auth_validator = SigV4Validator(credentials_store)

    server = NexusHTTPServer(
        nexus_fs=nx,
        auth_validator=auth_validator,
        host=host,
        port=port,
        bucket_name="nexus",
    )

    # Start in background
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Wait for server to start
    time.sleep(1)

    print(f"✓ Server started at http://{host}:{port}")
    return server, nx


def demo_boto3(endpoint_url, access_key, secret_key, bucket):
    """Demonstrate using boto3 with Nexus.

    Args:
        endpoint_url: Nexus server URL
        access_key: AWS access key
        secret_key: AWS secret key
        bucket: Bucket name
    """
    print("\n" + "=" * 60)
    print("Demo: Using boto3 with Nexus")
    print("=" * 60)

    # Create S3 client
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )

    # 1. Upload files
    print("\n1. Uploading files...")
    s3.put_object(Bucket=bucket, Key="demo/file1.txt", Body=b"Hello from boto3!")
    s3.put_object(Bucket=bucket, Key="demo/file2.txt", Body=b"Nexus S3 API works!")
    s3.put_object(Bucket=bucket, Key="demo/data/nested.txt", Body=b"Nested directories work too!")
    print("   ✓ Uploaded 3 files")

    # 2. List objects
    print("\n2. Listing objects...")
    response = s3.list_objects_v2(Bucket=bucket, Prefix="demo/")
    for obj in response["Contents"]:
        print(f"   - {obj['Key']} ({obj['Size']} bytes)")

    # 3. Download file
    print("\n3. Downloading file...")
    response = s3.get_object(Bucket=bucket, Key="demo/file1.txt")
    content = response["Body"].read()
    print(f"   Content: {content.decode('utf-8')}")

    # 4. Get metadata
    print("\n4. Getting file metadata...")
    response = s3.head_object(Bucket=bucket, Key="demo/file1.txt")
    print(f"   Size: {response['ContentLength']} bytes")
    print(f"   ETag: {response['ETag']}")
    print(f"   Last Modified: {response['LastModified']}")

    # 5. Delete file
    print("\n5. Deleting file...")
    s3.delete_object(Bucket=bucket, Key="demo/file2.txt")
    print("   ✓ Deleted demo/file2.txt")

    # 6. Verify deletion
    print("\n6. Verifying deletion...")
    response = s3.list_objects_v2(Bucket=bucket, Prefix="demo/")
    print(f"   Remaining files: {response['KeyCount']}")
    for obj in response["Contents"]:
        print(f"   - {obj['Key']}")


def demo_direct_nexus(nx):
    """Demonstrate direct Nexus API access.

    Args:
        nx: Nexus filesystem instance
    """
    print("\n" + "=" * 60)
    print("Demo: Direct Nexus API (same filesystem)")
    print("=" * 60)

    # Files created via S3 API are accessible via Nexus API
    print("\n1. Reading files created via S3 API...")
    content = nx.read("/demo/file1.txt")
    print(f"   /demo/file1.txt: {content.decode('utf-8')}")

    # And vice versa - files created via Nexus API are accessible via S3
    print("\n2. Creating file via Nexus API...")
    nx.write("/demo/nexus-native.txt", b"Created directly via Nexus API")
    print("   ✓ Created /demo/nexus-native.txt")

    # List all files
    print("\n3. Listing all files in /demo...")
    files = nx.list("/demo", recursive=True)
    for file_path in files:
        print(f"   - {file_path}")


def main():
    """Run the demo."""
    print("=" * 60)
    print("Nexus S3-Compatible API Demo")
    print("=" * 60)

    # Configuration
    host = "localhost"
    port = 8082
    endpoint_url = f"http://{host}:{port}"
    access_key = "demo-key"
    secret_key = "demo-secret"
    bucket = "nexus"

    # Start server
    print("\nStarting Nexus HTTP server...")
    server, nx = start_server_background(host, port)

    try:
        # Demo boto3
        demo_boto3(endpoint_url, access_key, secret_key, bucket)

        # Demo direct Nexus access
        demo_direct_nexus(nx)

        print("\n" + "=" * 60)
        print("Demo completed successfully!")
        print("=" * 60)

        # Show rclone config
        print("\nTo use with rclone, configure:")
        print("  rclone config create nexus s3 \\")
        print("    provider=Other \\")
        print(f"    endpoint={endpoint_url} \\")
        print(f"    access_key_id={access_key} \\")
        print(f"    secret_access_key={secret_key} \\")
        print("    force_path_style=true")

    finally:
        # Cleanup
        print("\nCleaning up...")
        server.shutdown()
        nx.delete("/demo/file1.txt")
        nx.delete("/demo/data/nested.txt")
        nx.delete("/demo/nexus-native.txt")
        nx.close()
        print("✓ Cleanup complete")


if __name__ == "__main__":
    main()
