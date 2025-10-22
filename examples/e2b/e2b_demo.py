#!/usr/bin/env python3
"""
E2B + Nexus Integration Demo

This example demonstrates how to use Nexus filesystem within E2B sandboxes
by mounting a remote Nexus server via FUSE. Once mounted, all standard Unix
tools and Python file I/O work seamlessly with Nexus.

Prerequisites:
    1. E2B CLI installed: npm install -g @e2b/cli
    2. E2B template built: e2b template build -c examples/e2b/Dockerfile
    3. E2B API key: export E2B_API_KEY=your-key
    4. Nexus server running (or use http://nexus.sudorouter.ai)

Usage:
    export E2B_API_KEY=your-e2b-key
    export NEXUS_API_KEY=your-nexus-key  # Optional
    python examples/e2b/e2b_demo.py
"""

import os
import sys

try:
    from e2b import Sandbox
except ImportError:
    print("Error: e2b package not installed")
    print("Install with: pip install e2b")
    sys.exit(1)


def main() -> None:
    # Configuration
    nexus_url = os.getenv("NEXUS_URL", "http://nexus.sudorouter.ai")
    nexus_api_key = os.getenv("NEXUS_API_KEY", "")
    template = os.getenv("E2B_TEMPLATE", "nexus-sandbox-v1")
    mount_path = "/home/user/nexus"

    print(f"Creating E2B sandbox with template: {template}")

    # Create sandbox with Nexus template
    # Note: E2B SDK 2.x uses .create() method
    sandbox = Sandbox.create(template=template)
    print(f"✓ Sandbox created: {sandbox.id}")

    try:
        # Build mount command
        mount_cmd = f"nexus mount {mount_path} --remote {nexus_url} --daemon"
        if nexus_api_key:
            mount_cmd += f" --api-key {nexus_api_key}"

        print(f"\nMounting Nexus from {nexus_url}...")
        result = sandbox.commands.run(mount_cmd)
        if result.exit_code != 0:
            print(f"✗ Mount failed: {result.stderr}")
            return

        # Verify mount
        result = sandbox.commands.run(f"ls -la {mount_path}")
        print(f"✓ Mounted successfully\n{result.stdout}")

        # Demo 1: Write file using echo
        print("\n=== Demo 1: Write file using echo ===")
        sandbox.commands.run(f'echo "Hello from E2B sandbox!" > {mount_path}/workspace/hello.txt')
        result = sandbox.commands.run(f"cat {mount_path}/workspace/hello.txt")
        print(f"Content: {result.stdout.strip()}")

        # Demo 2: Use grep to search
        print("\n=== Demo 2: Use grep to search ===")
        sandbox.commands.run(f'echo "TODO: Implement feature X" > {mount_path}/workspace/notes.txt')
        sandbox.commands.run(f'echo "DONE: Fixed bug Y" >> {mount_path}/workspace/notes.txt')
        sandbox.commands.run(f'echo "TODO: Write tests" >> {mount_path}/workspace/notes.txt')

        result = sandbox.commands.run(f"grep TODO {mount_path}/workspace/notes.txt")
        print(f"TODO items:\n{result.stdout}")

        # Demo 3: Use find to locate files
        print("=== Demo 3: Use find to locate files ===")
        result = sandbox.commands.run(f"find {mount_path}/workspace -name '*.txt'")
        print(f"Text files:\n{result.stdout}")

        # Demo 4: Python script accessing mounted files
        print("=== Demo 4: Python script with mounted files ===")
        python_script = f"""
import json

# Read from Nexus
with open('{mount_path}/workspace/hello.txt', 'r') as f:
    data = f.read()

# Process
result = {{'input': data.strip(), 'length': len(data), 'uppercase': data.upper().strip()}}

# Write back to Nexus
with open('{mount_path}/workspace/processed.json', 'w') as f:
    json.dump(result, f, indent=2)

print("Processed:", result)
"""
        result = sandbox.commands.run(f'python3 -c "{python_script}"')
        print(result.stdout)

        # Verify the JSON output
        result = sandbox.commands.run(f"cat {mount_path}/workspace/processed.json")
        print(f"Output JSON:\n{result.stdout}")

        # Demo 5: Word count on multiple files
        print("\n=== Demo 5: Word count across files ===")
        result = sandbox.commands.run(f"wc -l {mount_path}/workspace/*.txt")
        print(result.stdout)

        # Demo 6: Create directory and organize files
        print("=== Demo 6: Directory operations ===")
        sandbox.commands.run(f"mkdir -p {mount_path}/workspace/data")
        sandbox.commands.run(f"mv {mount_path}/workspace/*.txt {mount_path}/workspace/data/")
        result = sandbox.commands.run(
            f"tree {mount_path}/workspace || ls -R {mount_path}/workspace"
        )
        print(f"Directory structure:\n{result.stdout}")

        print("\n=== All demos completed successfully! ===")

    finally:
        # Cleanup
        print("\nCleaning up...")
        sandbox.commands.run(f"nexus unmount {mount_path} 2>/dev/null || true")
        sandbox.close()
        print("✓ Sandbox closed")


if __name__ == "__main__":
    main()
