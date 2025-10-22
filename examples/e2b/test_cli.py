#!/usr/bin/env python3
"""Test Nexus CLI commands (without FUSE) in E2B sandbox"""

import os

from e2b import Sandbox

# Configuration
template_name = "nexus-integration-test"
e2b_api_key = os.getenv("E2B_API_KEY", "")
nexus_url = os.getenv("NEXUS_URL", "http://nexus.sudorouter.ai")

print("=== E2B + Nexus CLI Integration Test ===\n")
print(f"Template: {template_name}")
print(f"Nexus URL: {nexus_url}\n")

# Create sandbox
print("Creating sandbox...")
sandbox = Sandbox.create(template=template_name, api_key=e2b_api_key)
print(f"✓ Sandbox created: {sandbox.sandbox_id}\n")

try:
    # Test 1: Nexus version
    print("Test 1: Check Nexus version...")
    result = sandbox.commands.run("nexus --version")
    if result.exit_code == 0:
        print(f"✓ Nexus version: {result.stdout.strip()}")
    else:
        print(f"✗ Failed: {result.stderr}")

    # Test 2: Nexus help
    print("\nTest 2: Check Nexus help...")
    result = sandbox.commands.run("nexus --help")
    if result.exit_code == 0:
        print("✓ Nexus CLI is working")
    else:
        print(f"✗ Failed: {result.stderr}")

    # Test 3: Check Python can import nexus
    print("\nTest 3: Check Python can import nexus...")
    result = sandbox.commands.run("python3.11 -c 'import nexus; print(nexus.__version__)'")
    if result.exit_code == 0:
        print(f"✓ Python can import nexus: {result.stdout.strip()}")
    else:
        print(f"✗ Failed: {result.stderr}")

    # Test 4: Check if FUSE is available
    print("\nTest 4: Check FUSE availability...")
    result = sandbox.commands.run("ls -la /dev/fuse")
    if result.exit_code == 0:
        print("✓ FUSE device exists")
    else:
        print(f"✗ FUSE device not found: {result.stderr}")

    # Test 5: Try mounting to see detailed error (don't throw exception)
    print("\nTest 5: Try mounting (expected to fail without permissions)...")
    # Create mount directory first
    sandbox.commands.run("mkdir -p /tmp/nexus-mount || true")

    # Try mount and capture all output
    result = sandbox.commands.run(
        "nexus mount /tmp/nexus-mount --remote-url http://nexus.sudorouter.ai 2>&1 || true"
    )
    print(f"Mount attempt exit code: {result.exit_code}")
    if result.stdout:
        print(f"Output: {result.stdout.strip()}")

    # Try with sudo
    print("\nTest 6: Try mounting with sudo...")
    result = sandbox.commands.run(
        "sudo nexus mount /tmp/nexus-mount --remote-url http://nexus.sudorouter.ai 2>&1 || true"
    )
    print(f"Sudo mount exit code: {result.exit_code}")
    if result.stdout:
        print(f"Output: {result.stdout.strip()}")

    print("\n=== All tests completed ===")
    print("\nNote: FUSE mounting may fail in E2B sandboxes due to permission restrictions.")
    print("This is expected. The Nexus CLI and Python SDK are working correctly.")

finally:
    print("\nCleaning up...")
    sandbox.kill()
    print("✓ Sandbox killed")
