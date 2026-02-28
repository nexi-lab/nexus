# Deployment Profiles & Modes Tutorial

This tutorial covers Nexus deployment profiles, server modes, and how to verify
that core filesystem operations work across all configurations.

## Concepts

### Profiles (NEXUS_PROFILE)

Profiles control which **bricks** (system services) are enabled:

| Profile | Bricks | Target |
|---------|--------|--------|
| `minimal` | 1 (storage only) | Bare minimum runnable |
| `embedded` | 2 (+ eventlog) | MCU / WASM (< 1 MB) |
| `lite` | 8 (+ namespace, agent registry, permissions, cache, ipc, scheduler) | Pi, Jetson, mobile (512 MB - 4 GB) |
| `full` | 21 (+ search, pay, llm, skills, sandbox, workflows, a2a, discovery, mcp, memory, observability, uploads, resiliency) | Desktop, laptop (4 - 32 GB) |
| `cloud` | 22 (+ federation) | K8s, serverless (unlimited) |
| `remote` | 0 (NFS-client model) | Client-side proxy |
| `auto` | auto-detected by RAM/CPU/GPU | Any device |

Profile hierarchy: `minimal` ⊂ `embedded` ⊂ `lite` ⊂ `full` ⊂ `cloud`. `remote` is orthogonal (zero bricks).

Core filesystem operations (write, read, stat, list, glob, grep) live in the **kernel** (`NexusFS`), not in bricks, so they work across all profiles.

### Modes (NEXUS_MODE)

Modes control the **deployment topology** and are orthogonal to profiles:

| Mode | Description |
|------|-------------|
| `standalone` | Single-node, local redb storage (default) |
| `federation` | Multi-zone Raft consensus via ZoneManager |
| `remote` | Client-side thin proxy (SDK/CLI only, not for servers) |

You can combine any profile with `standalone` or `federation` mode.

## Prerequisites

```bash
pip install -e .  # Install nexus-ai-fs from source
```

For federation mode, you also need the Rust extension built with full features:

```bash
# Requires protobuf 3.x (brew install protobuf@21)
PROTOC=/opt/homebrew/opt/protobuf@21/bin/protoc \
  maturin develop -m rust/nexus_raft/Cargo.toml --features full
```

## Tutorial 1: Start Server with Different Profiles

Each profile starts on its own port with an isolated data directory:

```bash
# Minimal (1 brick)
nexus serve --profile minimal --port 3026 --data-dir /tmp/nexus-tutorial/minimal

# Full (21 bricks)
nexus serve --profile full --port 3027 --data-dir /tmp/nexus-tutorial/full

# Cloud (22 bricks, includes federation brick)
nexus serve --profile cloud --port 3028 --data-dir /tmp/nexus-tutorial/cloud

# Auto-detect based on hardware
nexus serve --profile auto --port 3029 --data-dir /tmp/nexus-tutorial/auto
```

Verify with health check:

```bash
curl -s http://localhost:3026/health | python3 -m json.tool
# {"status": "healthy", "service": "nexus-rpc", ...}
```

Or run the automated script:

```bash
python3 examples/tutorials/deployment-profiles/test_profiles_serve.py
```

## Tutorial 2: Python SDK Operations Across Profiles

The SDK can connect directly (no server needed) for local testing:

```python
import nexus

nx = nexus.connect(config={"mode": "standalone", "data_dir": "/tmp/nexus-tutorial/sdk"})

# Write
nx.sys_write("/project/main.py", b'print("Hello, Nexus!")\n')

# Read (byte-exact round-trip)
content = nx.sys_read("/project/main.py")
assert content == b'print("Hello, Nexus!")\n'

# Stat
info = nx.sys_stat("/project/main.py")
print(f"path={info['path']}, size={info['size']}")

# List
entries = nx.sys_readdir("/project")
print(entries)  # ['/project/main.py']

# Glob
matches = nx.glob("**/*.py")
print(matches)  # ['/project/main.py']

# Grep
hits = nx.grep("Hello")
print(f"{len(hits)} match(es)")
```

Run the full test across all profiles:

```bash
python3 examples/tutorials/deployment-profiles/test_profiles_sdk.py
```

## Tutorial 3: Federation Mode

Federation mode uses Raft consensus for multi-zone metadata replication.
It works with any profile:

```bash
# Start with federation mode + lite profile
NEXUS_MODE=federation nexus serve --profile lite --port 3030 \
  --data-dir /tmp/nexus-tutorial/federation
```

Run the automated test:

```bash
python3 examples/tutorials/deployment-profiles/test_profiles_federation.py
```

## Tutorial 4: Remote Client Mode

Remote mode creates a thin gRPC proxy to a running server.

### Step 1: Start a server with gRPC enabled

```bash
NEXUS_GRPC_PORT=3051 nexus serve --profile full --port 3050 \
  --data-dir /tmp/nexus-tutorial/server
```

### Step 2: Connect with Python SDK

```python
import os, nexus

os.environ["NEXUS_GRPC_PORT"] = "3051"

nx = nexus.connect(config={
    "mode": "remote",
    "url": "http://localhost:3050",
})

# All operations proxy through gRPC to the server
nx.sys_write("/hello.txt", b"Written via gRPC!")
content = nx.sys_read("/hello.txt")
print(content)  # b'Written via gRPC!'
```

### Step 3: Or use the CLI

```bash
export NEXUS_URL=http://localhost:3050
export NEXUS_GRPC_PORT=3051

nexus write /hello.txt "Written via CLI remote!"
nexus cat /hello.txt
nexus ls /
nexus rm -f /hello.txt
```

Run the automated test:

```bash
python3 examples/tutorials/deployment-profiles/test_remote_client.py
```

## Cleanup

```bash
rm -rf /tmp/nexus-tutorial
```
