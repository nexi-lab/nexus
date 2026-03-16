# Deployment Profiles Tutorial

This tutorial covers Nexus deployment profiles and how to verify
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

### Topology

The `cloud` profile enables federation (multi-zone Raft consensus).
The `remote` profile creates a thin client proxy (SDK/CLI only, not for servers).
All other profiles run as single-node local storage by default.

## Prerequisites

```bash
pip install -e .  # Install nexus-ai-fs from source
```

For the `cloud` profile (federation), you also need the Rust extension built with full features:

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

nx = nexus.connect(config={"profile": "full", "data_dir": "/tmp/nexus-tutorial/sdk"})

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

## Tutorial 3: CLI Operations via gRPC (Client-Server)

The OS-style architecture: `nexus serve` runs as the kernel (persistent daemon),
and CLI commands are thin gRPC clients that make syscalls to it.

### Step 1: Start a server with gRPC enabled

```bash
NEXUS_GRPC_PORT=3051 nexus serve --profile minimal --port 3050 \
  --data-dir /tmp/nexus-tutorial/server
```

### Step 2: Run CLI commands as a remote client

```bash
export NEXUS_URL=http://localhost:3050
export NEXUS_GRPC_PORT=3051

# Write
nexus write /project/src/main.py '# TODO: implement
print("Hello, Nexus!")'

# Read
nexus cat /project/src/main.py

# Stat
nexus info /project/src/main.py

# List
nexus ls /project/src

# Glob
nexus glob "**/*.py"

# Grep
nexus grep "TODO"
```

Run the automated test across all profiles:

```bash
python3 examples/tutorials/deployment-profiles/test_profiles_cli.py
```

## Tutorial 4: Cloud Profile (Federation)

The `cloud` profile enables Raft consensus for multi-zone metadata replication:

```bash
# Start with cloud profile (federation enabled)
nexus serve --profile cloud --port 3030 \
  --data-dir /tmp/nexus-tutorial/federation
```

Run the automated test:

```bash
python3 examples/tutorials/deployment-profiles/test_profiles_federation.py
```

## Tutorial 5: Remote Client Profile (Python SDK)

The `remote` profile creates a thin gRPC proxy to a running server.

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
    "profile": "remote",
    "url": "http://localhost:3050",
})

# All operations proxy through gRPC to the server
nx.sys_write("/hello.txt", b"Written via gRPC!")
content = nx.sys_read("/hello.txt")
print(content)  # b'Written via gRPC!'
```

### Step 3: Or use the CLI (see Tutorial 3)

Run the automated test:

```bash
python3 examples/tutorials/deployment-profiles/test_remote_client.py
```

## Test Coverage Matrix

All core filesystem operations (write, read, stat, list, glob, grep) have been
verified across the following dimensions:

### By Profile × Client Interface

| Profile    | Python SDK (standalone) | Python SDK (remote) | CLI (gRPC) | Server startup |
|------------|:-----------------------:|:-------------------:|:----------:|:--------------:|
| `minimal`  | OK                      | OK                  | OK         | OK             |
| `embedded` | OK                      | —                   | OK         | OK             |
| `lite`     | OK                      | —                   | OK         | OK             |
| `full`     | OK                      | —                   | OK         | OK             |
| `cloud`    | OK                      | —                   | OK         | OK             |
| `remote`   | OK                      | —                   | OK         | OK             |
| `auto`     | OK                      | —                   | OK         | OK             |

Note: `remote` profile tested against a `minimal` server. The gRPC transport
is profile-agnostic so one server profile is sufficient.

### By Operation × Interface

| Operation | Python SDK (standalone) | Python SDK (remote) | CLI (gRPC)        |
|-----------|:-----------------------:|:-------------------:|:-----------------:|
| write     | OK                      | OK                  | OK                |
| read      | OK (byte-exact)         | OK (byte-exact)     | OK (content match)|
| stat      | OK (size verified)      | OK (size verified)  | OK (size present) |
| list      | OK (entries verified)   | OK (entries verified)| OK (entries match)|
| glob      | OK (3 .py files)        | OK (2 .py files)    | OK (3 .py files)  |
| grep      | OK (>= 2 matches)      | OK (>= 2 matches)   | OK (>= 2 matches) |
| delete    | —                       | OK (verified gone)  | —                 |

### Test Scripts

| Script                         | What it tests                                          |
|--------------------------------|--------------------------------------------------------|
| `test_profiles_serve.py`      | Server startup + health check, 7 profiles              |
| `test_profiles_sdk.py`        | 6 ops × 7 profiles, Python SDK standalone              |
| `test_profiles_cli.py`        | 6 ops × 7 profiles, CLI via gRPC against server        |
| `test_profiles_federation.py` | Server startup with federation mode, 7 profiles        |
| `test_remote_client.py`       | 8 ops via Python SDK remote client vs minimal server   |

## Cleanup

```bash
rm -rf /tmp/nexus-tutorial
```
