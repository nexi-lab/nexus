#!/usr/bin/env python3
"""End-to-end test of Nexus FUSE mount in E2B sandbox"""

import os

from e2b import Sandbox

# Configuration
template_id = "7ebpm01g5wtzdvlf75lx"
template_name = "nexus-integration-test"
nexus_url = os.getenv("NEXUS_URL", "http://nexus.sudorouter.ai")
nexus_api_key = os.getenv("NEXUS_API_KEY", "")
e2b_api_key = os.getenv("E2B_API_KEY", "")
mount_path = "/home/user/nexus"

print("=== E2B + Nexus FUSE Integration Test ===\n")
print(f"Template: {template_name} ({template_id})")
print(f"Nexus URL: {nexus_url}")
print(f"Mount path: {mount_path}\n")

# Create sandbox
print("Creating sandbox...")
sandbox = Sandbox.create(template=template_name, api_key=e2b_api_key)
print(f"✓ Sandbox created: {sandbox.sandbox_id}\n")

try:
    # Build mount command
    mount_cmd = f"nexus mount {mount_path} --remote-url {nexus_url} --daemon"
    if nexus_api_key:
        mount_cmd += f" --remote-api-key {nexus_api_key}"

    print("Mounting Nexus...")
    print(f"Command: {mount_cmd}\n")

    # Try mounting and capture any error
    try:
        result = sandbox.commands.run(mount_cmd, timeout=30000)
        print("✓ Mount command executed")
        print(f"stdout: {result.stdout}")
        print(f"stderr: {result.stderr}\n")
        mount_success = True
    except Exception as e:
        print(f"✗ Mount failed with exception: {e}")
        print(f"Error details: {str(e)}\n")
        mount_success = False

    if mount_success:
        print("✓ Mount command executed\n")

        # Test 1: List mount point
        print("Test 1: List mounted directory...")
        result = sandbox.commands.run(f"ls -la {mount_path}")
        if result.exit_code == 0:
            print("✓ Directory listing works:")
            print(result.stdout)
        else:
            print(f"✗ Failed: {result.stderr}\n")

        # Test 2: Write file with echo
        print("\nTest 2: Write file using echo...")
        result = sandbox.commands.run(f"echo 'Hello from E2B!' > {mount_path}/workspace/test.txt")
        if result.exit_code == 0:
            print("✓ Write succeeded")
        else:
            print(f"✗ Write failed: {result.stderr}")

        # Test 3: Read file with cat
        print("\nTest 3: Read file using cat...")
        result = sandbox.commands.run(f"cat {mount_path}/workspace/test.txt")
        if result.exit_code == 0:
            print(f"✓ Read succeeded: {result.stdout.strip()}")
        else:
            print(f"✗ Read failed: {result.stderr}")

        # Test 4: Use grep
        print("\nTest 4: Search with grep...")
        result = sandbox.commands.run(f"grep 'E2B' {mount_path}/workspace/test.txt")
        if result.exit_code == 0:
            print(f"✓ Grep works: {result.stdout.strip()}")
        else:
            print(f"✗ Grep failed: {result.stderr}")

        print("\n=== All tests completed ===")

finally:
    print("\nCleaning up...")
    sandbox.commands.run(f"nexus unmount {mount_path} 2>/dev/null || true")
    sandbox.kill()
    print("✓ Sandbox killed")
