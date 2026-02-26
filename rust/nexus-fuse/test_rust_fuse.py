#!/usr/bin/env python3
"""Build and test Rust FUSE client in E2B sandbox."""

import asyncio
import os
import time

from e2b import AsyncSandbox


async def main() -> None:
    print("=" * 60)
    print("Rust FUSE Client Build & Benchmark")
    print("=" * 60)

    nexus_url = os.environ.get("NEXUS_URL", "https://44cd12366a42.ngrok-free.app")
    nexus_api_key = os.environ.get(
        "NEXUS_API_KEY", "sk-default_admin_4084c44e_9b690b93b23dfc1ef135032dbf9f33b7"
    )

    print(f"NEXUS_URL: {nexus_url}")
    print()

    # Create sandbox
    print("Creating sandbox...")
    t0 = time.time()
    sandbox = await AsyncSandbox.create(template="base", timeout=900)
    print(f"  Created: {sandbox.sandbox_id} ({(time.time() - t0) * 1000:.1f}ms)")

    try:
        # Install Rust
        print("\n=== Installing Rust ===")
        t0 = time.time()
        result = await sandbox.commands.run(
            'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y', timeout=120
        )
        print(f"  Rust installed: {result.exit_code} ({(time.time() - t0) * 1000:.1f}ms)")

        # Install fuse-dev
        print("\n=== Installing fuse-dev ===")
        t0 = time.time()
        result = await sandbox.commands.run(
            "sudo apt-get update && sudo apt-get install -y libfuse-dev pkg-config", timeout=120
        )
        print(f"  fuse-dev installed: {result.exit_code} ({(time.time() - t0) * 1000:.1f}ms)")

        # Upload source
        print("\n=== Uploading source ===")
        base_path = os.path.dirname(os.path.abspath(__file__))
        if not base_path.endswith("nexus-fuse"):
            base_path = os.path.join(os.path.dirname(base_path), "nexus-fuse")

        await sandbox.commands.run("mkdir -p /home/user/nexus-fuse/src")

        files = [
            ("Cargo.toml", "Cargo.toml"),
            ("src/main.rs", "src/main.rs"),
            ("src/client.rs", "src/client.rs"),
            ("src/fs.rs", "src/fs.rs"),
        ]

        for local_name, remote_name in files:
            local_path = os.path.join(base_path, local_name)
            with open(local_path) as f:
                content = f.read()
            await sandbox.files.write(f"/home/user/nexus-fuse/{remote_name}", content)
            print(f"  Uploaded: {local_name}")

        # Build
        print("\n=== Building Rust binary (this takes ~2-3 min) ===")
        t0 = time.time()
        result = await sandbox.commands.run(
            ". ~/.cargo/env && cd /home/user/nexus-fuse && cargo build --release 2>&1", timeout=300
        )
        build_time = (time.time() - t0) * 1000
        print(f"  Build: exit={result.exit_code} ({build_time:.1f}ms)")

        if result.exit_code != 0:
            print(f"  Error: {result.stderr}")
            return

        # Check binary
        result = await sandbox.commands.run(
            "ls -la /home/user/nexus-fuse/target/release/nexus-fuse"
        )
        print(f"  Binary: {result.stdout.strip()}")

        # Copy to /usr/local/bin
        await sandbox.commands.run(
            "sudo cp /home/user/nexus-fuse/target/release/nexus-fuse /usr/local/bin/"
        )

        # Test startup time
        print("\n=== Rust Binary Startup Time ===")
        result = await sandbox.commands.run(
            "time /usr/local/bin/nexus-fuse version 2>&1", timeout=10
        )
        print(f"  {result.stdout}")

        # Benchmark FUSE mount
        print("\n=== Rust FUSE Mount Benchmark ===")

        # Create mount point
        t0 = time.time()
        await sandbox.commands.run("sudo mkdir -p /mnt/nexus")
        print(f"  mkdir: {(time.time() - t0) * 1000:.1f}ms")

        # Mount using Rust binary
        mount_cmd = f"""
nohup sudo NEXUS_URL="{nexus_url}" NEXUS_API_KEY="{nexus_api_key}" \
    /usr/local/bin/nexus-fuse mount /mnt/nexus \
    --url "{nexus_url}" --api-key "{nexus_api_key}" \
    --allow-other -f > /tmp/mount.log 2>&1 &
"""
        t0 = time.time()
        await sandbox.commands.run(mount_cmd, timeout=10)
        print(f"  Start mount: {(time.time() - t0) * 1000:.1f}ms")

        # Poll for mount
        t0 = time.time()
        attempts = 0
        mounted = False
        for _ in range(20):
            attempts += 1
            result = await sandbox.commands.run("mount | grep nexus || true", timeout=5)
            if "nexus" in result.stdout:
                mounted = True
                break
            await asyncio.sleep(0.2)
        print(
            f"  Poll mount: {(time.time() - t0) * 1000:.1f}ms ({attempts} attempts, mounted={mounted})"
        )

        if not mounted:
            result = await sandbox.commands.run("cat /tmp/mount.log", timeout=5)
            print(f"  Mount log: {result.stdout}")
            return

        # List files
        t0 = time.time()
        result = await sandbox.commands.run("ls -la /mnt/nexus/", timeout=10)
        print(f"  ls /mnt/nexus: {(time.time() - t0) * 1000:.1f}ms")
        print(f"  Files: {result.stdout}")

        # Compare with Python version if installed
        print("\n=== Comparison with Python nexus ===")
        result = await sandbox.commands.run(
            "which nexus 2>/dev/null || echo 'not installed'", timeout=5
        )
        if "not installed" not in result.stdout:
            t0 = time.time()
            result = await sandbox.commands.run("time nexus --version 2>&1", timeout=30)
            print(f"  Python nexus --version: {(time.time() - t0) * 1000:.1f}ms")
        else:
            print("  Python nexus not installed (skipping comparison)")

    finally:
        await sandbox.kill()
        print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
