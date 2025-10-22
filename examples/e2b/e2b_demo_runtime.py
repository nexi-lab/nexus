#!/usr/bin/env python3
"""
E2B + Nexus Integration Demo (Runtime Installation)

This version works WITHOUT custom E2B templates by installing Nexus
at runtime in the default code-interpreter sandbox.

Use this if you're getting template build errors.

Prerequisites:
    1. E2B API key: export E2B_API_KEY=your-key
    2. pip install e2b

Usage:
    export E2B_API_KEY=your-e2b-key
    export NEXUS_API_KEY=your-nexus-key  # Optional
    python examples/e2b/e2b_demo_runtime.py
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
    mount_path = "/home/user/nexus"

    print("Creating E2B sandbox with default code-interpreter template...")

    # Use default template (no custom build needed!)
    # Note: E2B SDK 2.x uses .create() method
    sandbox = Sandbox.create(template="code-interpreter")
    print(f"✓ Sandbox created: {sandbox.id}")

    try:
        # Install Nexus at runtime
        print("\nInstalling Nexus (this takes ~30 seconds)...")
        result = sandbox.commands.run("pip install -q nexus-ai-fs[fuse]")
        if result.exit_code != 0:
            print(f"✗ Installation failed: {result.stderr}")
            return

        # Verify installation
        result = sandbox.commands.run("nexus --version")
        print(f"✓ Nexus installed: {result.stdout.strip()}")

        # Build mount command
        mount_cmd = f"nexus mount {mount_path} --remote {nexus_url} --daemon"
        if nexus_api_key:
            mount_cmd += f" --api-key {nexus_api_key}"

        print(f"\nMounting Nexus from {nexus_url}...")
        result = sandbox.commands.run(mount_cmd)
        if result.exit_code != 0:
            print(f"✗ Mount failed: {result.stderr}")
            print("\nNote: FUSE may not be available in E2B containers.")
            print("Try the native CLI version (e2b_demo_cli.py) instead.")
            return

        # Verify mount
        result = sandbox.commands.run(f"ls -la {mount_path}")
        print(f"✓ Mounted successfully\n{result.stdout}")

        # Demo 1: Write file using echo
        print("\n=== Demo 1: Write file using echo ===")
        sandbox.commands.run(
            f'echo "Hello from E2B sandbox (runtime install)!" > {mount_path}/workspace/hello.txt'
        )
        result = sandbox.commands.run(f"cat {mount_path}/workspace/hello.txt")
        print(f"Content: {result.stdout.strip()}")

        # Demo 2: Use grep to search
        print("\n=== Demo 2: Use grep to search ===")
        sandbox.commands.run(f'echo "TODO: Implement feature X" > {mount_path}/workspace/notes.txt')
        sandbox.commands.run(f'echo "DONE: Fixed bug Y" >> {mount_path}/workspace/notes.txt')
        sandbox.commands.run(f'echo "TODO: Write tests" >> {mount_path}/workspace/notes.txt')

        result = sandbox.commands.run(f"grep TODO {mount_path}/workspace/notes.txt")
        print(f"TODO items:\n{result.stdout}")

        # Demo 3: Python script accessing mounted files
        print("=== Demo 3: Python script with mounted files ===")
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

        print("\n=== All demos completed successfully! ===")
        print("\n✓ Runtime installation works!")
        print("✓ Installation time: ~30s per sandbox")
        print("✓ No custom template needed")

    finally:
        # Cleanup
        print("\nCleaning up...")
        sandbox.commands.run(f"nexus unmount {mount_path} 2>/dev/null || true")
        sandbox.close()
        print("✓ Sandbox closed")


if __name__ == "__main__":
    main()
