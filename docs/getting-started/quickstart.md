# Quickstart

Nexus = filesystem/context plane.

This page documents the local SDK path verified against this repository on March 11, 2026 using Python 3.13. The package metadata supports Python 3.12+.

## Prerequisites

- Python 3.12 or newer
- A writable local directory for Nexus state

## Run A Verified Example

From a checkout:

```bash
PYTHONPATH=src python3.13 - <<'PY'
from nexus.sdk import connect

nx = connect(
    config={
        "profile": "minimal",
        "data_dir": "./nexus-data",
    }
)

nx.sys_write("/hello.txt", b"Hello, Nexus!")
print(nx.sys_read("/hello.txt").decode())

nx.close()
PY
```

From PyPI, install `nexus-ai-fs` first and run the same snippet without
`PYTHONPATH=src`.

Expected output:

```text
Hello, Nexus!
```

## Local CLI Quickstart

This standalone CLI path was also verified in this repository on March 11, 2026.

From a checkout:

```bash
PYTHONPATH=src python3.13 -m nexus.cli.main init .nexus-cli-demo
export NEXUS_DATA_DIR="$PWD/.nexus-cli-demo/nexus-data"

PYTHONPATH=src python3.13 -m nexus.cli.main write /workspace/hello.txt "hello from cli"
PYTHONPATH=src python3.13 -m nexus.cli.main cat /workspace/hello.txt
PYTHONPATH=src python3.13 -m nexus.cli.main ls /workspace
```

If you installed from PyPI, use `nexus` instead of `python3.13 -m nexus.cli.main`.

Expected result:

- `write` succeeds against the workspace-scoped data dir.
- `cat` returns `hello from cli`.
- `ls` shows `/workspace/hello.txt`.

## Why This Quickstart

- It does not require `nexusd`.
- It does not require API keys.
- It does not require the full federation build.
- It exercises the real local `connect()` path used by the SDK and CLI.

## Next Steps

- Stay local and keep building from the [Local SDK path](../paths/embedded-sdk.md).
- Move to a service deployment with the [Shared daemon path](../paths/daemon-and-remote.md).
- Read the system model in [Architecture](../paths/architecture.md).
- Read the [trust boundary guide](trust-boundary.md).
