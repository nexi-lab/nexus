#!/usr/bin/env python3
"""Check if Nexus S3 server is running and accessible."""

import sys

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError


def check_server(endpoint_url: str, access_key: str, secret_key: str) -> bool:
    """Check if server is accessible."""
    print(f"Checking server at: {endpoint_url}")
    print("=" * 60)

    try:
        # Create S3 client
        s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )

        # Try to list buckets/objects
        print("Attempting to connect...")
        response = s3.list_objects_v2(Bucket="nexus", MaxKeys=1)

        print("✅ Server is RUNNING and accessible!")
        print("   Status: Connected successfully")

        if "Contents" in response:
            print(f"   Objects found: {response['KeyCount']}")
        else:
            print("   No objects in bucket")

        return True

    except EndpointConnectionError as e:
        print("❌ Server is NOT accessible")
        print(f"   Error: Cannot connect to {endpoint_url}")
        print(f"   Details: {e}")
        return False

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")

        if error_code == "403" or "AccessDenied" in str(e):
            print("⚠️  Server is running but authentication failed")
            print("   Error: Invalid credentials")
            return True  # Server is running, just auth issue
        else:
            print("❌ Server error")
            print(f"   Error: {e}")
            return False

    except Exception as e:
        print("❌ Unexpected error")
        print(f"   Error: {e}")
        return False


if __name__ == "__main__":
    # Default values - override with command line args
    endpoint = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
    access_key = sys.argv[2] if len(sys.argv) > 2 else "testkey"
    secret_key = sys.argv[3] if len(sys.argv) > 3 else "testsecret"

    print("""
Usage: python check_server.py [ENDPOINT] [ACCESS_KEY] [SECRET_KEY]

Examples:
  python check_server.py http://localhost:8080 testkey testsecret
  python check_server.py http://your-server.com:8080 mykey mysecret

""")

    check_server(endpoint, access_key, secret_key)
